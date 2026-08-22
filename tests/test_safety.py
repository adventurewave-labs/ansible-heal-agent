"""Tests for the two PRD MUSTs that previously had no implementing code.

NFR-2 — the agent never writes outside ANSIBLE_HEAL_ALLOWED_PATHS.
NFR-3 — the agent never touches the base branch without human approval.
"""

from __future__ import annotations

import subprocess

import pytest

from agent import patcher
from agent.core import MODE_DRY_RUN, MODE_PR, heal


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=False).stdout.strip()


def _self_targeting_fix():
    """A fix that would rewrite the agent's own source."""
    return {
        "action": "edit_file",
        "target_file": "agent/core.py",
        "search": "def heal(",
        "replace": "def heal_pwned(",
    }


# ── NFR-2: write surface ───────────────────────────────────────────────

def test_patcher_refuses_to_write_outside_the_allowlist(scratch_repo):
    victim = scratch_repo / "agent"
    victim.mkdir(parents=True, exist_ok=True)
    (victim / "core.py").write_text("def heal(): pass\n")

    with pytest.raises(patcher.PathNotAllowed) as exc:
        patcher.apply_fix(_self_targeting_fix())

    assert "agent/core.py" in str(exc.value)
    assert (victim / "core.py").read_text() == "def heal(): pass\n"


def test_allowlist_denial_is_checked_before_the_file_is_even_probed(scratch_repo):
    """A denied path must not leak existence information via a different error."""
    with pytest.raises(patcher.PathNotAllowed):
        patcher.apply_fix({
            "action": "edit_file",
            "target_file": "somewhere/definitely_absent.yml",
            "search": "a", "replace": "b",
        })


def test_traversal_out_of_the_repo_is_denied(scratch_repo):
    with pytest.raises(patcher.PathNotAllowed):
        patcher.apply_fix({
            "action": "edit_file",
            "target_file": "../escape.yml",
            "search": "a", "replace": "b",
        })


def test_widening_the_allowlist_permits_the_write(scratch_repo, monkeypatch):
    monkeypatch.setenv("ANSIBLE_HEAL_ALLOWED_PATHS", "ansible/**,infra/**")
    (scratch_repo / "infra").mkdir()
    target = scratch_repo / "infra" / "main.yml"
    target.write_text("key: old\n")

    patcher.apply_fix({"action": "edit_file", "target_file": "infra/main.yml",
                       "search": "old", "replace": "new"})
    assert target.read_text() == "key: new\n"


def test_empty_allowlist_denies_every_write(scratch_repo, monkeypatch):
    monkeypatch.setenv("ANSIBLE_HEAL_ALLOWED_PATHS", "")
    inv = scratch_repo / "ansible" / "inventory.yml"
    before = inv.read_text()
    with pytest.raises(patcher.PathNotAllowed):
        patcher.apply_fix({"action": "edit_file",
                           "target_file": "ansible/inventory.yml",
                           "search": "web-01:", "replace": "web-99:"})
    assert inv.read_text() == before


def test_blocked_paths_are_reported_not_silently_skipped(scratch_repo, monkeypatch):
    monkeypatch.setenv("ANSIBLE_HEAL_ALLOWED_PATHS", "")
    result = heal(max_retries=3, use_llm=False)
    assert not result.success
    assert result.blocked, "the run must report why it could not patch"
    assert "allowed write surface" in result.blocked[0]


def test_a_stalled_run_stops_instead_of_burning_its_retry_budget(
        scratch_repo, monkeypatch):
    """With every write denied, iteration 1 cannot differ from iteration 0."""
    monkeypatch.setenv("ANSIBLE_HEAL_ALLOWED_PATHS", "")
    result = heal(max_retries=3, use_llm=False)
    assert result.iterations == 0, "should stop after the first fruitless pass"


# ── dry run ────────────────────────────────────────────────────────

def test_dry_run_writes_nothing_and_commits_nothing(scratch_repo, git_log_count):
    inv = scratch_repo / "ansible" / "inventory.yml"
    before_inv = inv.read_text()
    before_commits = git_log_count()

    result = heal(mode=MODE_DRY_RUN, use_llm=False)

    assert inv.read_text() == before_inv
    assert git_log_count() == before_commits
    assert result.proposals, "a dry run must still report what it would do"


def test_dry_run_proposals_carry_a_real_diff(scratch_repo):
    result = heal(mode=MODE_DRY_RUN, use_llm=False)
    inv = [p for p in result.proposals if p.target_file == "ansible/inventory.yml"]
    assert inv, "expected an inventory proposal"
    diff = inv[0].diff
    assert diff.startswith("--- a/ansible/inventory.yml")
    assert "-        web-01:" in diff
    assert "+        web-server-01:" in diff


def test_dry_run_does_not_claim_success(scratch_repo):
    result = heal(mode=MODE_DRY_RUN, use_llm=False)
    assert result.success is False
    assert result.mode == MODE_DRY_RUN


def test_patcher_dry_run_flag_leaves_the_file_alone(scratch_repo):
    inv = scratch_repo / "ansible" / "inventory.yml"
    before = inv.read_text()
    patch = patcher.apply_fix({
        "action": "edit_file", "target_file": "ansible/inventory.yml",
        "search": "web-01:", "replace": "web-server-01:",
    }, dry_run=True)
    assert inv.read_text() == before
    assert patch["applied"] is False
    assert patch["diff"]


# ── NFR-3: PR mode ─────────────────────────────────────────────────

def test_pr_mode_leaves_the_base_branch_untouched(scratch_repo, git_log_count):
    base_before = _git(scratch_repo, "rev-parse", "HEAD")
    commits_before = git_log_count()

    result = heal(mode=MODE_PR, use_llm=False)

    assert _git(scratch_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(scratch_repo, "rev-parse", "HEAD") == base_before
    assert git_log_count() == commits_before
    assert result.branch and result.branch.startswith("heal/")


def test_pr_mode_puts_the_commits_on_the_heal_branch(scratch_repo):
    result = heal(mode=MODE_PR, use_llm=False)
    count = _git(scratch_repo, "rev-list", "--count", result.branch)
    base = _git(scratch_repo, "rev-list", "--count", "main")
    assert int(count) == int(base) + 3


def test_pr_mode_does_not_re_run_the_pipeline(scratch_repo):
    """The whole point of approval mode: stop and wait for a human."""
    result = heal(mode=MODE_PR, use_llm=False)
    assert result.iterations == 0
    assert len(result.history) == 1


def test_pr_mode_reports_the_missing_remote_rather_than_failing_silently(scratch_repo):
    result = heal(mode=MODE_PR, use_llm=False)
    assert result.pushed is False
    assert "no remote" in (result.push_error or "")


def test_pr_mode_with_nothing_patchable_returns_to_base(scratch_repo, monkeypatch):
    monkeypatch.setenv("ANSIBLE_HEAL_ALLOWED_PATHS", "")
    result = heal(mode=MODE_PR, use_llm=False)
    assert _git(scratch_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert result.push_error == "no patches applied; nothing to open a PR for"


# ── mode validation ───────────────────────────────────────────────

def test_unknown_mode_is_rejected(scratch_repo):
    with pytest.raises(ValueError):
        heal(mode="yolo", use_llm=False)
