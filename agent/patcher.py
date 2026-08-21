"""Patcher — apply a diagnosis's fix to the target repository.

Supports a single action: ``edit_file`` (bounded string replace). The action
contract is intentionally tiny so the LLM has only one thing to propose.

Three gates run before anything is written, in this order:

1. **Path allowlist** (PRD NFR-2). The target must match one of
   ``ANSIBLE_HEAL_ALLOWED_PATHS`` (default ``ansible/**``). Without this an
   LLM-proposed ``target_file`` of ``agent/core.py`` would be applied — the
   agent could rewrite itself.
2. **Traversal check.** The resolved path must stay inside the repo root.
3. **YAML validation.** The patched content must still parse. If it does not,
   nothing is written and PatchError is raised so the caller can fall back.

``dry_run=True`` runs all three gates and returns the diff without writing.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

import yaml

from agent.config import ConfigError, allowed_paths, is_path_allowed, repo_root, resolve_in_repo


class PatchError(RuntimeError):
    pass


class PathNotAllowed(PatchError):
    """Raised when a fix targets a file outside the configured write surface."""


def _validate_yaml(path: Path, content: str) -> None:
    """Raise PatchError if ``content`` is not valid YAML for the given path."""
    if path.suffix not in (".yml", ".yaml"):
        return
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise PatchError(f"patched {path.name} is invalid YAML: {e}") from e


def check_allowed(target_rel: str) -> None:
    """Raise PathNotAllowed if ``target_rel`` is outside the write surface."""
    if not is_path_allowed(target_rel):
        raise PathNotAllowed(
            f"refusing to write {target_rel}: outside the allowed write surface "
            f"{list(allowed_paths())}. Set ANSIBLE_HEAL_ALLOWED_PATHS to widen it."
        )


def apply_fix(fix: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    """Apply ``fix`` to the target repo and return a result record.

    With ``dry_run=True`` every check still runs and the diff is still computed,
    but the file is left untouched and ``["applied"]`` is False.
    """
    action = fix.get("action", "none")
    if action == "none":
        return {"target_file": None, "diff": "", "ok": True,
                "applied": False, "note": "no-op fix"}

    if action != "edit_file":
        raise PatchError(f"Unknown action: {action}")

    target_rel = fix.get("target_file")
    if not target_rel:
        raise PatchError("fix is missing target_file")

    # Gate 1: allowlist. Before any filesystem access, so a denied path is not
    # even probed for existence.
    check_allowed(target_rel)

    # Gate 2: traversal.
    try:
        target = resolve_in_repo(target_rel)
    except ConfigError as e:
        raise PathNotAllowed(str(e)) from e

    if not target.exists():
        raise PatchError(f"target_file does not exist: {target_rel}")

    original = target.read_text()
    search = fix.get("search")
    replace = fix.get("replace")
    if search is None or replace is None:
        raise PatchError("edit_file fix requires both 'search' and 'replace'")

    if search not in original:
        raise PatchError(
            f"search substring not found in {target_rel}. "
            f"The file may already be patched, or the proposed anchor is wrong."
        )

    occurrences = original.count(search)
    patched = original.replace(search, replace, 1)

    # Gate 3: the result must still be parseable. Raising here means nothing
    # was written, so the tree is untouched and the caller can fall back.
    _validate_yaml(target, patched)

    if not dry_run:
        target.write_text(patched)

    return {
        "target_file": target_rel,
        "occurrences_found": occurrences,
        "before_lines": search.count("\n") + 1,
        "after_lines": replace.count("\n") + 1,
        "ok": True,
        "applied": not dry_run,
        "dry_run": dry_run,
        "diff": unified_diff(original, patched, target_rel),
    }


def unified_diff(before: str, after: str, path: str = "") -> str:
    """Return a real unified diff, not a hand-rolled approximation."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}" if path else "before",
            tofile=f"b/{path}" if path else "after",
            n=3,
        )
    ).rstrip("\n")


def repo_relative(path: Path) -> str:
    return str(Path(path).resolve().relative_to(repo_root().resolve()))
