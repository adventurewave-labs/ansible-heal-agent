"""Git helper — the small set of git operations the agent needs.

All operations are scoped to ``agent.config.repo_root()``, resolved per call, so
the agent can act on an arbitrary IaC repository (and so tests can point it at a
scratch repo instead of the developer's working tree).
"""

from __future__ import annotations

import fcntl
import subprocess
import time
from contextlib import contextmanager
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


class GitStateError(RuntimeError):
    """The repository is mid-operation, or git refused to commit."""


@contextmanager
def exclusive_lock(timeout: float = 60.0):
    """Serialise agent runs against one repository.

    Git's index is a single shared file. Two heal() processes on the same repo
    interleaved `add` and `commit`, so one run's staged file was swept into the
    other's commit, and edits were routinely left staged but never committed.
    """
    root = repo_root()
    lock_path = root / ".git" / "ansible-heal.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    handle = lock_path.open("w")
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise GitStateError(
                        f"another ansible-heal run holds the lock on {root}; "
                        f"waited {timeout:.0f}s") from None
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


#: Files git leaves in .git/ while an operation is half-finished. Committing
#: during one of these concludes the operator's merge or rebase for them,
#: under a commit subject about something else entirely, with their conflict
#: resolution buried inside it.
_IN_PROGRESS_MARKERS = (
    ("MERGE_HEAD", "a merge is in progress"),
    ("CHERRY_PICK_HEAD", "a cherry-pick is in progress"),
    ("REVERT_HEAD", "a revert is in progress"),
    ("REBASE_HEAD", "a rebase is in progress"),
    ("rebase-merge", "an interactive rebase is in progress"),
    ("rebase-apply", "a rebase or am is in progress"),
    ("BISECT_LOG", "a bisect is in progress"),
)


def in_progress_operation() -> str | None:
    """Describe any half-finished git operation, or None if the tree is idle."""
    git_dir = repo_root() / ".git"
    for name, description in _IN_PROGRESS_MARKERS:
        if (git_dir / name).exists():
            return description
    return None


def is_detached() -> bool:
    """True when HEAD is not on a branch. Commits made here are unreachable."""
    return _git("symbolic-ref", "--quiet", "HEAD").returncode != 0


def add(path: str | Path) -> None:
    _git("add", str(path))


def commit(message: str, allow_empty: bool = False) -> str:
    """Create a commit and return its SHA.

    Raises GitStateError rather than returning "" when git refuses. The empty
    string was indistinguishable from a real SHA to every caller: a failing
    pre-commit hook, a submodule, an ignored path or an unresolved conflict all
    produced a run that reported success, recorded a phantom commit, and left
    the agent's edits staged but never committed.
    """
    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    proc = _git(*args)
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or
                  "git refused the commit").splitlines()
        raise GitStateError(
            f"could not commit: {detail[0] if detail else 'unknown reason'}")
    sha = _git("rev-parse", "HEAD").stdout.strip()
    if not sha:
        raise GitStateError("commit reported success but HEAD did not move")
    return sha


def has_commits() -> bool:
    """False on a freshly initialised repo whose HEAD is unborn."""
    return _git("rev-parse", "--verify", "HEAD").returncode == 0


def current_branch() -> str:
    """The checked-out branch name.

    ``rev-parse --abbrev-ref HEAD`` prints the literal string ``HEAD`` on an
    unborn branch, which PR mode then tried to check back out — leaving the
    user on the heal branch with their base branch gone. ``symbolic-ref``
    reports the real name before the first commit exists.
    """
    proc = _git("symbolic-ref", "--short", "HEAD")
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
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
