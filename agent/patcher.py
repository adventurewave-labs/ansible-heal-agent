"""Patcher — apply a diagnosis's fix to the repo tree.

Supports a single action today: ``edit_file`` (string replace). The action
contract is intentionally tiny so the LLM has only one thing to propose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.git_helper import REPO_ROOT


class PatchError(RuntimeError):
    pass


def apply_fix(fix: dict[str, Any]) -> dict[str, Any]:
    """Apply the fix dict to the repo and return a small result record.

    Returns ``{"target_file": str, "diff": str, "ok": bool, "error": str?}``.
    """
    action = fix.get("action", "none")
    if action == "none":
        return {"target_file": None, "diff": "", "ok": True, "note": "no-op fix"}

    if action != "edit_file":
        raise PatchError(f"Unknown action: {action}")

    target_rel = fix["target_file"]
    target = REPO_ROOT / target_rel
    if not target.exists():
        raise PatchError(f"target_file does not exist: {target_rel}")

    original = target.read_text()
    search = fix["search"]
    replace = fix["replace"]

    if search not in original:
        raise PatchError(
            f"search substring not found in {target_rel}. "
            f"This usually means the file was already patched or the LLM guessed wrong."
        )

    occurrences = original.count(search)
    patched = original.replace(search, replace, 1)
    target.write_text(patched)

    return {
        "target_file": target_rel,
        "occurrences_replaced": occurrences,
        "before_lines": search.count("\n") + 1,
        "after_lines": replace.count("\n") + 1,
        "ok": True,
        "diff": _mini_diff(original, patched),
    }


def _mini_diff(before: str, after: str) -> str:
    """Produce a unified-diff-like string for transcript readability."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    out = []
    seen_change = False
    for i in range(max(len(before_lines), len(after_lines))):
        b = before_lines[i] if i < len(before_lines) else ""
        a = after_lines[i] if i < len(after_lines) else ""
        if b == a:
            if seen_change:
                out.append(f"  {b}")
        elif b and a:
            out.append(f"- {b}")
            out.append(f"+ {a}")
            seen_change = True
        elif b:
            out.append(f"- {b}")
            seen_change = True
        elif a:
            out.append(f"+ {a}")
            seen_change = True
    return "\n".join(out[-50:])  # last 50 lines max
