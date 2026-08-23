"""Unit tests for the pipeline runner, scanner, diagnoser and patcher.

Every test that writes anything runs against the ``scratch_repo`` fixture, so
the suite never mutates or commits to the developer's checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import diagnoser, log_scanner, patcher
from pipeline import runner


def _site(repo: Path) -> Path:
    return repo / "ansible" / "playbooks" / "site.yml"


# ── mock runner ───────────────────────────────────────────

def test_baseline_fails_with_three_failures(scratch_repo):
    result = runner.run_pipeline(_site(scratch_repo))
    assert result.exit_code != 0
    types = sorted({f["type"] for f in result.failures})
    assert types == ["removed_module", "undefined_variable", "unreachable_host"]


def test_log_scanner_extracts_all_failure_types(scratch_repo):
    result = runner.run_pipeline(_site(scratch_repo))
    failures = log_scanner.extract_failures(result.log_path)
    types = {f["type"] for f in failures}
    assert types >= {"unreachable_host", "removed_module", "undefined_variable"}


def test_runner_accepts_relative_playbook_path(scratch_repo, monkeypatch):
    """Regression: relative paths used to crash in relative_to(repo_root())."""
    monkeypatch.chdir(scratch_repo)
    result = runner.run_pipeline(Path("ansible/playbooks/site.yml"))
    assert result.exit_code == 2


def test_runner_goes_green_once_baseline_is_fixed(scratch_repo):
    inv = scratch_repo / "ansible" / "inventory.yml"
    inv.write_text(inv.read_text().replace("web-01:", "web-server-01:"))
    gv = scratch_repo / "ansible" / "group_vars" / "all.yml"
    gv.write_text(gv.read_text() + "\nnginx_port: 8080\n")
    web = scratch_repo / "ansible" / "playbooks" / "webservers.yml"
    # community.docker.docker_container is the real migration target, but it
    # lives in a collection this environment does not install — and the
    # runner now asks ansible-doc about every module a task uses, not a
    # hardcoded pair of names, so that name alone would still be reported
    # broken here (see
    # test_the_mock_pipeline_does_not_report_a_false_green_after_an_unresolvable_swap
    # in test_destructive_inputs.py). Any module ansible-doc genuinely
    # resolves demonstrates the same thing this test checks: the runner
    # reports green once every input is actually fixed.
    web.write_text(web.read_text().replace(
        "ansible.builtin.docker:", "ansible.builtin.debug:"))

    result = runner.run_pipeline(_site(scratch_repo))
    assert result.exit_code == 0, result.failures
    assert result.failures == []


# ── diagnoser ───────────────────────────────────────────

def test_fallback_diagnoser_hostname_change(scratch_repo):
    diag = diagnoser.fallback_diagnose(
        {"type": "unreachable_host", "host": "web-server-01", "message": "x"})
    assert diag["failure_type"] == "unreachable_host"
    assert diag["fix"]["action"] == "rename_host"
    assert diag["fix"]["target_file"] == "ansible/inventory.yml"
    # The stale host is found by comparing against the real inventory.
    assert diag["fix"]["old"] == "web-01"
    assert diag["fix"]["new"] == "web-server-01"


def test_fallback_diagnoser_removed_module(scratch_repo):
    """`docker`, not `apt_key`: the agent asks ansible-doc first and refuses to
    migrate a module that still resolves, so apt_key no longer reaches here."""
    diag = diagnoser.fallback_diagnose(
        {"type": "removed_module", "module": "docker", "message": "x"})
    assert diag["failure_type"] == "removed_module"
    assert diag["fix"]["action"] == "replace_module"
    assert diag["fix"]["old_module"] == "docker"
    assert diag["fix"]["new_module"] == "community.docker.docker_container"
    # The playbook it targets is discovered, not hardcoded.
    assert diag["fix"]["target_file"] == "ansible/playbooks/webservers.yml"


def test_a_module_that_still_resolves_is_not_migrated(scratch_repo):
    """apt_key resolves on ansible-core 2.19. Rewriting a working playbook —
    a key import became a file download, dropping `id:` and `state:` — is a
    modernisation the operator chooses, not a repair an agent commits."""
    diag = diagnoser.fallback_diagnose(
        {"type": "removed_module", "module": "apt_key", "message": "x"})
    assert diag["fix"]["action"] == "none"
    assert "resolves" in diag["_no_fix_reason"]


def test_fallback_diagnoser_undefined_var(scratch_repo):
    diag = diagnoser.fallback_diagnose(
        {"type": "undefined_variable", "variable": "nginx_port", "message": "x"})
    assert diag["fix"]["action"] == "set_yaml_key"
    assert diag["fix"]["target_file"] == "ansible/group_vars/all.yml"
    assert diag["fix"]["key"] == "nginx_port"
    assert diag["fix"]["value"] == 8080


def test_unknown_failure_type_yields_no_op_fix(scratch_repo):
    diag = diagnoser.fallback_diagnose({"type": "meteor_strike"})
    assert diag["fix"]["action"] == "none"


# ── patcher ───────────────────────────────────────────

def test_patcher_applies_hostname_rename(scratch_repo):
    diag = diagnoser.fallback_diagnose(
        {"type": "unreachable_host", "host": "web-server-01", "message": "x"})
    patch = patcher.apply_fix(diag["fix"])
    assert patch["ok"]
    inventory = (scratch_repo / "ansible" / "inventory.yml").read_text()
    assert "web-server-01:" in inventory
    assert "web-01:" not in inventory
    # The other host and its vars survive the structural edit.
    assert "web-02:" in inventory
    assert "10.0.1.22" in inventory


def test_patcher_rejects_second_apply(scratch_repo):
    diag = diagnoser.fallback_diagnose(
        {"type": "unreachable_host", "host": "web-server-01", "message": "x"})
    patcher.apply_fix(diag["fix"])
    with pytest.raises(patcher.PatchError):
        patcher.apply_fix(diag["fix"])


def test_patcher_rejects_missing_target(scratch_repo):
    with pytest.raises(patcher.PatchError):
        patcher.apply_fix({"action": "edit_file", "target_file": "ansible/nope.yml",
                           "search": "a", "replace": "b"})


def test_patcher_rejects_invalid_yaml_and_does_not_write(scratch_repo):
    inv = scratch_repo / "ansible" / "inventory.yml"
    before = inv.read_text()
    with pytest.raises(patcher.PatchError):
        patcher.apply_fix({
            "action": "edit_file",
            "target_file": "ansible/inventory.yml",
            "search": "web-01:",
            "replace": "web-01: [unclosed",
        })
    assert inv.read_text() == before


# ── heal loop ───────────────────────────────────────────

def test_full_heal_loop_with_fallback(scratch_repo, git_log_count):
    """The seeded baseline has three failure classes. The host rename and the
    undefined variable both resolve cleanly. The module class does not: its
    replacement, `community.docker.docker_container`, lives in a collection
    this environment does not install, so the mock pipeline — now honestly
    checking every module against ansible-doc rather than a fixed two-name
    dict — correctly keeps reporting it broken after the swap, matching what
    `ansible-playbook` itself would say (see test_real_ansible.py). The loop
    still makes all three fixes, one commit each; it does not claim success
    over the one it cannot actually verify."""
    from agent.core import heal
    before = git_log_count()
    result = heal(playbook="ansible/playbooks/site.yml", max_retries=3,
                  use_llm=False, transcript=None)
    assert not result.success
    assert any("would change nothing" in d for d in result.declined), result.declined
    assert git_log_count() == before + 3, "expected exactly one commit per fix"


def test_heal_on_green_repo_is_a_no_op(scratch_repo, git_log_count):
    """A second run makes no further commits: the module class still cannot
    resolve without a collection this environment does not install, and the
    patcher's own no-op guard refuses to re-propose the identical swap."""
    from agent.core import heal
    heal(playbook="ansible/playbooks/site.yml", max_retries=3, use_llm=False)
    after_first = git_log_count()

    result = heal(playbook="ansible/playbooks/site.yml", max_retries=3, use_llm=False)
    assert not result.success
    assert result.iterations == 0
    assert git_log_count() == after_first, "must not commit a no-op fix twice"


def test_heal_leaves_developer_checkout_untouched(scratch_repo):
    """The agent must write inside the target repo only."""
    import subprocess

    from agent.core import heal
    own = Path(__file__).resolve().parent.parent
    before = subprocess.run(["git", "status", "--porcelain"], cwd=own,
                            capture_output=True, text=True).stdout
    heal(playbook="ansible/playbooks/site.yml", max_retries=3, use_llm=False)
    after = subprocess.run(["git", "status", "--porcelain"], cwd=own,
                           capture_output=True, text=True).stdout
    assert before == after
