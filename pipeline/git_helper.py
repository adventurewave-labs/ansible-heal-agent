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


def git_dir() -> Path:
    """The real .git directory, which is a *file* in a worktree or submodule."""
    proc = _git("rev-parse", "--absolute-git-dir")
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return repo_root() / ".git"


def is_repo() -> bool:
    """True if the target already belongs to a git repository.

    `(root / ".git").is_dir()` is False in a worktree and in a submodule, where
    `.git` is a file pointing elsewhere. Treating those as "not a repository"
    made the agent `git init` on top of a real one.

    "Inside a work tree" is not sufficient either: a scratch directory created
    *under* a repository but ignored by it — the demo workspace, a tmpdir in a
    checkout — is inside one and belongs to none, and commits aimed at it would
    silently stage nothing. Such a directory gets its own repository; an
    ordinary subdirectory keeps using the enclosing one.
    """
    if _git("rev-parse", "--is-bare-repository").stdout.strip() == "true":
        # A bare repo has no work tree, so the check below says "not a
        # repository" and the agent would `git init` a shadow .git inside it,
        # masking every ref in the real one from anything that clones the path.
        return True
    proc = _git("rev-parse", "--is-inside-work-tree")
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        return False
    top = _git("rev-parse", "--show-toplevel").stdout.strip()
    if top and Path(top).resolve() == repo_root().resolve():
        return True
    return _git("check-ignore", "-q", ".").returncode != 0


def init_if_needed() -> None:
    """Ensure a git repo exists at the target root, with an identity configured."""
    if not is_repo():
        _git("init", "-b", "main")
        _git("config", "user.email", "agent@ansible-heal.local")
        _git("config", "user.name", "Ansible Heal Agent")
        # Deliberately NOT `git add .` + commit. Initialising someone's
        # directory is already presumptuous; sweeping every file in it into a
        # commit authored by the agent — a stray .env, a work-in-progress file
        # — is not the agent's call to make. The agent's own commits stage only
        # the files it actually edits.
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
    lock_dir = git_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "ansible-heal.lock"
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
    marker_dir = git_dir()
    for name, description in _IN_PROGRESS_MARKERS:
        if (marker_dir / name).exists():
            return description
    return None


def is_detached() -> bool:
    """True when HEAD is not on a branch. Commits made here are unreachable."""
    return _git("symbolic-ref", "--quiet", "HEAD").returncode != 0


#: Paths already modified in the working tree when the run started.
_PREEXISTING_DIRTY: set[str] = set()


def snapshot_dirty() -> None:
    """Record the working tree's modified paths before the agent edits anything."""
    global _PREEXISTING_DIRTY
    proc = _git("status", "--porcelain")
    paths: set[str] = set()
    for line in proc.stdout.splitlines():
        if len(line) > 3:
            paths.add(line[3:].strip().split(" -> ")[-1])
    _PREEXISTING_DIRTY = paths


def was_dirty(path: str | Path) -> bool:
    """True if ``path`` already had uncommitted changes when the run started."""
    return str(path) in _PREEXISTING_DIRTY


def add(path: str | Path) -> None:
    _git("add", str(path))


def commit(message: str, allow_empty: bool = False,
           pathspec: str | None = None) -> str:
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
    if pathspec:
        # Commit ONLY this path. A bare `git commit` commits the whole index,
        # so pointing the agent at a subdirectory of a repository swept the
        # operator's own staged work into a commit titled as an Ansible fix.
        args.extend(["--", pathspec])
    proc = _git(*args)
    if proc.returncode != 0:
        # git puts the actual reason last ("nothing added to commit"), after
        # branch and status preamble. Taking the first line reported
        # "On branch master", which tells the operator nothing.
        lines = [ln.strip() for ln in
                 (proc.stderr + "\n" + proc.stdout).splitlines() if ln.strip()]
        noise = ("on branch", "your branch", "untracked files",
                 "changes not staged", "no changes added")
        useful = [ln for ln in lines
                  if not any(ln.lower().startswith(n) for n in noise)]
        raise GitStateError(
            f"could not commit: {useful[-1] if useful else 'git refused the commit'}")
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
