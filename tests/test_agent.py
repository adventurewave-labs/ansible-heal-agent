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


# ── mock runner ───────────────────────────────────────────────────────

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
    web.write_text(web.read_text().replace(
        "ansible.builtin.apt_key:", "ansible.builtin.get_url:"))

    result = runner.run_pipeline(_site(scratch_repo))
    assert result.exit_code == 0, result.failures
    assert result.failures == []


# ── diagnoser (pure functions — no filesystem writes) ────────────────────────

def test_fallback_diagnoser_hostname_change(scratch_repo):
    diag = diagnoser.fallback_diagnose(
        {"type": "unreachable_host", "host": "web-server-01", "message": "x"})
    assert diag["failure_type"] == "unreachable_host"
    assert diag["fix"]["target_file"] == "ansible/inventory.yml"


def test_fallback_diagnoser_removed_module(scratch_repo):
    diag = diagnoser.fallback_diagnose(
        {"type": "removed_module", "module": "apt_key", "message": "x"})
    assert diag["failure_type"] == "removed_module"
    assert "apt_key" in diag["fix"]["search"]


def test_fallback_diagnoser_undefined_var(scratch_repo):
    diag = diagnoser.fallback_diagnose(
        {"type": "undefined_variable", "variable": "nginx_port", "message": "x"})
    assert diag["fix"]["target_file"] == "ansible/group_vars/all.yml"
    assert "nginx_port" in diag["fix"]["replace"]


def test_unknown_failure_type_yields_no_op_fix(scratch_repo):
    diag = diagnoser.fallback_diagnose({"type": "meteor_strike"})
    assert diag["fix"]["action"] == "none"


# ── patcher ────────────────────────────────────────────────────────

def test_patcher_applies_hostname_rename(scratch_repo):
    diag = diagnoser.fallback_diagnose(
        {"type": "unreachable_host", "host": "web-server-01", "message": "x"})
    patch = patcher.apply_fix(diag["fix"])
    assert patch["ok"]
    assert "web-server-01" in (scratch_repo / "ansible" / "inventory.yml").read_text()


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


# ── heal loop ──────────────────────────────────────────────────────

def test_full_heal_loop_with_fallback(scratch_repo, git_log_count):
    from agent.core import heal
    before = git_log_count()
    result = heal(playbook="ansible/playbooks/site.yml", max_retries=3,
                  use_llm=False, transcript=None)
    assert result.success, f"heal failed: exit={result.final_exit_code}"
    assert git_log_count() == before + 3, "expected exactly one commit per fix"


def test_heal_on_green_repo_is_a_no_op(scratch_repo, git_log_count):
    from agent.core import heal
    heal(playbook="ansible/playbooks/site.yml", max_retries=3, use_llm=False)
    after_first = git_log_count()

    result = heal(playbook="ansible/playbooks/site.yml", max_retries=3, use_llm=False)
    assert result.success
    assert result.iterations == 0
    assert git_log_count() == after_first, "idempotent re-run must not commit"


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
