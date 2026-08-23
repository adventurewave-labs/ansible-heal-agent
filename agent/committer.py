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

    # Stage the resolved path. When the declared target is a symlink to another
    # file inside the write surface, the write goes through the link, so the
    # declared path's own blob is unchanged and git had nothing to commit —
    # leaving the agent's edit sitting in the working tree, uncommitted.
    staged = _resolved_target(target)
    # `git commit -- <path>` commits the WORKING TREE state of that path, not
    # what the agent staged. If the operator had their own uncommitted edits in
    # the same file, those went into a commit titled as an automated fix — in
    # one case a staging bump the operator had marked "do not ship".
    dirty = git_helper.was_dirty(staged)
    if dirty:
        raise git_helper.GitStateError(
            f"{staged} had uncommitted changes before the agent edited it; "
            f"committing now would include them under this fix's subject. "
            f"Commit or stash them and re-run")
    git_helper.add(staged)
    msg = _build_message(fix, diagnosis)
    return git_helper.commit(msg, pathspec=staged)


def resolved_target(target: str) -> str:
    """Public alias — core checks the same path the committer will stage."""
    return _resolved_target(target)


def _resolved_target(target: str) -> str:
    """``target``, or what it points at when it is a symlink inside the repo."""
    from agent.config import repo_root
    path = repo_root() / target
    try:
        if path.is_symlink():
            return str(path.resolve().relative_to(repo_root().resolve()))
    except (OSError, ValueError):
        pass
    return target


def commit_guard() -> str | None:
    """Why committing right now would be wrong, or None if it is safe.

    Checked before the agent writes anything, not after: discovering a merge in
    progress once the edits are staged is too late to be polite about.
    """
    busy = git_helper.in_progress_operation()
    if busy:
        return (f"{busy} in the target repository; the agent will not commit "
                f"into it. Finish or abort that operation and re-run.")
    if git_helper.is_detached():
        return ("the target repository has a detached HEAD; commits made here "
                "would not be reachable from any branch. Check out a branch "
                "and re-run.")
    return None


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
