"""Git helper — wrap the small set of git ops the agent needs.

All operations are scoped to the repo root (the parent of this file). This
wrapper exists so the agent never has to know git internals and so tests can
substitute a fake committer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def init_if_needed() -> None:
    """Ensure a git repo exists at REPO_ROOT."""
    if not (REPO_ROOT / ".git").is_dir():
        _git("init", "-b", "main")
        _git("config", "user.email", "agent@ansible-heal.local")
        _git("config", "user.name", "Ansible Heal Agent")
        _git("add", ".")
        _git("commit", "-m", "chore: initial baseline (broken state)")
    else:
        # Ensure identity is set (containers may not have it)
        if not _git("config", "user.email").stdout.strip():
            _git("config", "user.email", "agent@ansible-heal.local")
            _git("config", "user.name", "Ansible Heal Agent")


def add(path: str | Path) -> None:
    p = str(path)
    _git("add", p)


def commit(message: str, allow_empty: bool = False) -> str:
    """Create a commit and return its SHA."""
    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    res = _git(*args)
    if res.returncode != 0:
        # Maybe nothing to commit
        return ""
    sha = _git("rev-parse", "HEAD").stdout.strip()
    return sha


def diff_staged() -> str:
    return _git("diff", "--cached").stdout


def log(limit: int = 20) -> str:
    return _git("log", f"-{limit}", "--pretty=format:%h %ad %s", "--date=short").stdout


def is_clean() -> bool:
    res = _git("status", "--porcelain")
    return res.stdout.strip() == ""
