"""Integration tests against a real ``ansible-playbook`` process.

These are the tests that decide whether ``PIPELINE_RUNNER=real`` is a feature
or a stub. They shell out to the actual binary — no mock runner, no simulated
log strings — and assert the agent detects, and then heals, failures that a
real ansible-core produces.

Skipped when ansible-core is not installed; CI installs it.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from agent import config
from agent.core import MODE_DRY_RUN, heal
from pipeline import runner

pytestmark = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None,
    reason="ansible-core not installed",
)


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))


@pytest.fixture
def real_repo(scratch_repo: Path, monkeypatch) -> Path:
    """A repo whose playbooks real ansible-playbook can actually execute.

    Uses local connections so no SSH is involved, and reproduces the three
    failure classes the agent claims to handle, in the form real ansible-core
    actually reports them.
    """
    monkeypatch.setenv("PIPELINE_RUNNER", "real")

    _write(scratch_repo, "ansible/inventory.yml", """
        all:
          children:
            webservers:
              hosts:
                web-01:
                  ansible_connection: local
    """)
    _write(scratch_repo, "ansible/group_vars/all.yml", """
        ---
        env: production
    """)
    # Play 1: host pattern matches nothing. Real ansible WARNS and exits 0.
    _write(scratch_repo, "ansible/playbooks/webservers.yml", """
        - name: Configure webservers
          hosts: web-server-01
          gather_facts: false
          tasks:
            - name: Touch a marker
              ansible.builtin.debug:
                msg: configured
    """)
    # Play 2: undefined variable. Real ansible exits 2.
    _write(scratch_repo, "ansible/playbooks/vars.yml", """
        - name: Render config
          hosts: web-01
          gather_facts: false
          tasks:
            - name: Render listen port
              ansible.builtin.debug:
                msg: "listen {{ nginx_port }}"
    """)
    # Play 3: unresolvable module. Parse-time; real ansible exits 4 and no
    # callback ever fires.
    _write(scratch_repo, "ansible/playbooks/module.yml", """
        - name: Legacy module
          hosts: web-01
          gather_facts: false
          tasks:
            - name: Removed module
              ansible.builtin.docker:
                name: x
    """)
    _write(scratch_repo, "ansible/playbooks/site.yml", """
        - import_playbook: webservers.yml
    """)
    return scratch_repo


def _run(repo: Path, playbook: str):
    return runner.run_pipeline(repo / "ansible" / "playbooks" / playbook)


# ── detection ───────────────────────────────────────────────────────

def test_real_runner_detects_a_host_pattern_that_matches_nothing(real_repo):
    """The case the old regexes could never catch: real ansible exits 0."""
    result = _run(real_repo, "webservers.yml")

    assert result.failures, "a silently skipped play must not look healthy"
    assert result.failures[0]["type"] == "no_hosts_matched"
    assert result.exit_code == 2, "reported as a failure despite ansible's exit 0"


def test_real_runner_detects_an_undefined_variable_with_its_name(real_repo):
    result = _run(real_repo, "vars.yml")

    undefined = [f for f in result.failures if f["type"] == "undefined_variable"]
    assert undefined, result.failures
    assert undefined[0]["variable"] == "nginx_port"
    assert undefined[0]["host"] == "web-01"


def test_real_runner_detects_a_parse_time_module_error(real_repo):
    """Exit 4, aborts before any callback fires — text scan is the only source."""
    result = _run(real_repo, "module.yml")

    modules = [f for f in result.failures if f["type"] == "removed_module"]
    assert modules, result.failures
    assert modules[0]["module"] == "ansible.builtin.docker"
    assert modules[0]["module_short"] == "docker"


def test_real_runner_never_reports_failure_without_a_failure(real_repo):
    """The old run_real returned failures=[] for every run, so the agent no-oped."""
    for playbook in ("webservers.yml", "vars.yml", "module.yml"):
        result = _run(real_repo, playbook)
        if result.exit_code != 0:
            assert result.failures, f"{playbook}: nonzero exit with nothing to fix"


def test_real_runner_reports_a_green_run_as_green(real_repo):
    _write(real_repo, "ansible/playbooks/fine.yml", """
        - name: Fine
          hosts: web-01
          gather_facts: false
          tasks:
            - name: Say hello
              ansible.builtin.debug:
                msg: hello
    """)
    result = _run(real_repo, "fine.yml")
    assert result.exit_code == 0
    assert result.failures == []
    assert "web-01" in result.succeeded_hosts


def test_real_runner_writes_a_log_and_a_sidecar(real_repo):
    result = _run(real_repo, "vars.yml")
    log = real_repo / result.log_path
    assert log.exists()
    assert log.with_suffix(".json").exists()


# ── healing ────────────────────────────────────────────────────────

def test_agent_heals_a_real_ansible_run(real_repo):
    """End to end: real ansible-playbook fails, the agent patches, it goes green."""
    result = heal(playbook="ansible/playbooks/site.yml", max_retries=3,
                  use_llm=False)

    assert result.success, f"exit={result.final_exit_code} blocked={result.blocked}"
    inventory = (real_repo / "ansible" / "inventory.yml").read_text()
    assert "web-server-01:" in inventory
    assert "web-01:" not in inventory

    # And prove it against the binary directly, not via our own bookkeeping.
    proc = subprocess.run(
        ["ansible-playbook", "-i", "ansible/inventory.yml",
         "ansible/playbooks/site.yml"],
        cwd=real_repo, capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "ok=1" in proc.stdout


def test_dry_run_against_real_ansible_writes_nothing(real_repo):
    before = (real_repo / "ansible" / "inventory.yml").read_text()
    result = heal(playbook="ansible/playbooks/site.yml", mode=MODE_DRY_RUN,
                  use_llm=False)
    assert result.proposals
    assert (real_repo / "ansible" / "inventory.yml").read_text() == before


# ── configuration errors ─────────────────────────────────────────────

def test_requesting_the_real_runner_without_ansible_is_an_error(
        scratch_repo, monkeypatch):
    """Silently running the mock instead would be undetectable to an operator."""
    monkeypatch.setenv("PIPELINE_RUNNER", "real")
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    with pytest.raises(runner.RunnerUnavailable):
        runner.run_pipeline(config.repo_root() / "ansible" / "playbooks" / "site.yml")
