"""Unit tests for the pipeline runner."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import runner, git_helper
from agent import log_scanner, diagnoser, patcher

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_repo():
    """Make sure each test starts from the broken baseline."""
    from scenarios.seed import reset_to_baseline
    reset_to_baseline()
    yield


def test_baseline_fails_with_three_failures():
    result = runner.run_pipeline(REPO_ROOT / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code != 0
    assert len(result.failures) >= 3, "expected at least 3 seeded failures"
    types = sorted({f["type"] for f in result.failures})
    assert "unreachable_host" in types
    assert "removed_module" in types
    assert "undefined_variable" in types


def test_log_scanner_extracts_all_failure_types():
    result = runner.run_pipeline(REPO_ROOT / "ansible" / "playbooks" / "site.yml")
    failures = log_scanner.extract_failures(result.log_path)
    assert len(failures) >= 3
    types = {f["type"] for f in failures}
    assert types >= {"unreachable_host", "removed_module", "undefined_variable"}


def test_fallback_diagnoser_hostname_change():
    failure = {"type": "unreachable_host", "host": "web-server-01", "message": "x"}
    diag = diagnoser.fallback_diagnose(failure)
    assert diag["failure_type"] == "unreachable_host"
    assert diag["fix"]["target_file"] == "ansible/inventory.yml"
    assert diag["fix"]["search"] == "web-01:"


def test_fallback_diagnoser_removed_module():
    failure = {"type": "removed_module", "module": "apt_key", "message": "x"}
    diag = diagnoser.fallback_diagnose(failure)
    assert diag["failure_type"] == "removed_module"
    assert "apt_key" in diag["fix"]["search"]


def test_fallback_diagnoser_undefined_var():
    failure = {"type": "undefined_variable", "variable": "nginx_port", "message": "x"}
    diag = diagnoser.fallback_diagnose(failure)
    assert diag["fix"]["target_file"] == "ansible/group_vars/all.yml"
    assert "nginx_port" in diag["fix"]["replace"]


def test_patcher_applies_hostname_rename():
    failure = {"type": "unreachable_host", "host": "web-server-01", "message": "x"}
    diag = diagnoser.fallback_diagnose(failure)
    patch = patcher.apply_fix(diag["fix"])
    assert patch["ok"]
    new_inv = (REPO_ROOT / "ansible" / "inventory.yml").read_text()
    assert "web-server-01" in new_inv


def test_patcher_idempotent_after_apply():
    failure = {"type": "unreachable_host", "host": "web-server-01", "message": "x"}
    diag = diagnoser.fallback_diagnose(failure)
    patcher.apply_fix(diag["fix"])
    # Second apply should fail because search is no longer present
    with pytest.raises(patcher.PatchError):
        patcher.apply_fix(diag["fix"])


def test_full_heal_loop_with_fallback():
    """Heal loop using only the fallback diagnoser should converge in ≤3 iters."""
    from agent.core import heal
    result = heal(
        playbook="ansible/playbooks/site.yml",
        max_retries=3,
        use_llm=False,
        transcript=None,
    )
    assert result.success, f"heal loop failed: final_exit={result.final_exit_code}"
