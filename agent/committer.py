"""Committer — stage the patched file and create a conventional-commit."""
from __future__ import annotations

from typing import Any

from pipeline import git_helper


def commit_fix(fix: dict[str, Any], diagnosis: dict[str, Any]) -> str:
    """Stage the file touched by `fix` and commit with a conventional message.

    Returns the commit SHA (or empty string if nothing was staged).
    """
    target = fix.get("target_file")
    if not target or fix.get("action") == "none":
        return ""

    git_helper.add(target)
    msg = _build_message(fix, diagnosis)
    return git_helper.commit(msg)


def _build_message(fix: dict, diagnosis: dict) -> str:
    ftype = diagnosis.get("failure_type", "other")
    target = fix.get("target_file", "")
    scope = _scope_for_file(target)
    summary = _summary_for_type(ftype, diagnosis)
    body = diagnosis.get("diagnosis", "")
    rationale = fix.get("rationale", "")
    return (
        f"fix({scope}): {summary}\n\n"
        f"Root cause: {body}\n"
        f"Rationale: {rationale}\n\n"
        f"Patch applied by ansible-heal-agent."
    )


def _scope_for_file(path: str) -> str:
    if "inventory" in path:
        return "inventory"
    if "group_vars" in path:
        return "vars"
    if "playbooks" in path:
        return "playbook"
    return "repo"


def _summary_for_type(ftype: str, diagnosis: dict) -> str:
    # no_hosts_matched is what *real* Ansible produces for a stale host pattern;
    # unreachable_host is the simulator's name for the same class. Without both,
    # every real-runner host fix fell through to "apply automated fix" and the
    # conventional-commit subjects only ever appeared under the mock.
    if ftype in ("unreachable_host", "no_hosts_matched"):
        return "rename host to match playbook expectation"
    if ftype == "removed_module":
        return "migrate deprecated module to modern equivalent"
    if ftype == "undefined_variable":
        return "add missing variable to group_vars"
    return "apply automated fix"
