"""Git helper — the small set of git operations the agent needs.

All operations are scoped to ``agent.config.repo_root()``, resolved per call, so
the agent can act on an arbitrary IaC repository (and so tests can point it at a
scratch repo instead of the developer's working tree).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent.config import repo_root


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd or repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )


def init_if_needed() -> None:
    """Ensure a git repo exists at the target root, with an identity configured."""
    root = repo_root()
    if not (root / ".git").is_dir():
        _git("init", "-b", "main")
        _git("config", "user.email", "agent@ansible-heal.local")
        _git("config", "user.name", "Ansible Heal Agent")
        _git("add", ".")
        _git("commit", "-m", "chore: initial baseline")
    elif not _git("config", "user.email").stdout.strip():
        # Containers frequently have no global identity configured.
        _git("config", "user.email", "agent@ansible-heal.local")
        _git("config", "user.name", "Ansible Heal Agent")


def add(path: str | Path) -> None:
    _git("add", str(path))


def commit(message: str, allow_empty: bool = False) -> str:
    """Create a commit and return its SHA (empty string if nothing was staged)."""
    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    if _git(*args).returncode != 0:
        return ""
    return _git("rev-parse", "HEAD").stdout.strip()


def current_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def create_branch(name: str) -> bool:
    """Create and check out ``name``. Returns False if git refused."""
    return _git("checkout", "-b", name).returncode == 0


def checkout(name: str) -> bool:
    return _git("checkout", name).returncode == 0


def push(remote: str, branch: str) -> subprocess.CompletedProcess:
    """Push ``branch`` to ``remote``. The caller inspects returncode/stderr."""
    return _git("push", "--set-upstream", remote, branch)


def has_remote(name: str = "origin") -> bool:
    return name in _git("remote").stdout.split()


def diff_staged() -> str:
    return _git("diff", "--cached").stdout


def diff_unstaged() -> str:
    return _git("diff").stdout


def log(limit: int = 20) -> str:
    return _git("log", f"-{limit}", "--pretty=format:%h %ad %s", "--date=short").stdout


def head_sha() -> str:
    return _git("rev-parse", "HEAD").stdout.strip()


def is_clean() -> bool:
    return _git("status", "--porcelain").stdout.strip() == ""
