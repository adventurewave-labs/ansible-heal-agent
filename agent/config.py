"""Central runtime configuration for the heal agent.

Everything that used to be a module-level ``REPO_ROOT`` constant lives here and
is resolved at *call* time. That matters for three reasons:

1. The agent must be able to operate on an arbitrary IaC repository, not only
   on its own checkout. ``--repo`` / ``ANSIBLE_HEAL_REPO_ROOT`` set that target.
2. Tests must be able to point the agent at a scratch repo so running the suite
   never mutates or commits to the developer's working tree.
3. The write surface must be constrained (PRD NFR-2). ``allowed_paths()`` and
   ``is_path_allowed()`` are enforced by ``agent.patcher`` before any write.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Globs the patcher may write to when ANSIBLE_HEAL_ALLOWED_PATHS is unset.
DEFAULT_ALLOWED_PATHS: tuple[str, ...] = ("ansible/**",)

#: Set by ``repo_root_override``; takes precedence over the environment.
_OVERRIDE: Path | None = None

#: Directory containing this package (the agent's own checkout).
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


def repo_root() -> Path:
    """Return the repository the agent is operating on.

    Resolution order: explicit override → ``ANSIBLE_HEAL_REPO_ROOT`` → the
    agent's own checkout (the historical behaviour, kept so the bundled demo
    works with no configuration).
    """
    if _OVERRIDE is not None:
        return _OVERRIDE
    env = os.environ.get("ANSIBLE_HEAL_REPO_ROOT")
    if env:
        p = Path(env).expanduser().resolve()
        if not p.is_dir():
            raise ConfigError(f"ANSIBLE_HEAL_REPO_ROOT is not a directory: {p}")
        return p
    return _PACKAGE_ROOT


@contextmanager
def repo_root_override(path: str | Path) -> Iterator[Path]:
    """Temporarily point the agent at ``path``. Used by the CLI and by tests."""
    global _OVERRIDE
    previous = _OVERRIDE
    _OVERRIDE = Path(path).expanduser().resolve()
    try:
        yield _OVERRIDE
    finally:
        _OVERRIDE = previous


def set_repo_root(path: str | Path) -> Path:
    """Point the agent at ``path`` for the rest of the process."""
    global _OVERRIDE
    _OVERRIDE = Path(path).expanduser().resolve()
    return _OVERRIDE


#: Where run logs and transcripts are written. ``None`` means "inside the repo",
#: which is what apply and pr modes want. Dry-run points this at a scratch
#: directory outside the repo so that "writes nothing" is literally true.
_OUTPUT_ROOT: Path | None = None


def set_output_root(path: str | Path | None) -> Path | None:
    """Redirect run logs and transcripts away from the repo, or back into it."""
    global _OUTPUT_ROOT
    _OUTPUT_ROOT = None if path is None else Path(path).expanduser().resolve()
    return _OUTPUT_ROOT


def output_root_override() -> Path | None:
    """The explicit artefact root, if one is set. ``None`` means "the repo"."""
    return _OUTPUT_ROOT


def output_root() -> Path:
    """Root for agent-generated artefacts. Defaults to the repo itself."""
    return _OUTPUT_ROOT if _OUTPUT_ROOT is not None else repo_root()


def runs_dir() -> Path:
    """Directory pipeline run logs are written to (created on demand)."""
    d = output_root() / "pipeline" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def transcripts_dir() -> Path:
    d = output_root() / "transcripts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def allowed_paths() -> tuple[str, ...]:
    """Globs the patcher is permitted to write to, relative to ``repo_root()``.

    Configured via ``ANSIBLE_HEAL_ALLOWED_PATHS`` as a comma-separated list of
    globs. Empty entries are ignored. An explicitly empty value denies all
    writes, which is a useful way to run the agent purely as a reporter.
    """
    raw = os.environ.get("ANSIBLE_HEAL_ALLOWED_PATHS")
    if raw is None:
        return DEFAULT_ALLOWED_PATHS
    globs = tuple(g.strip() for g in raw.split(",") if g.strip())
    return globs


def is_path_allowed(rel_path: str | Path, globs: tuple[str, ...] | None = None) -> bool:
    """Return True if ``rel_path`` (repo-relative) matches an allowed glob.

    ``**`` is treated as "this directory and everything under it", which is what
    operators expect from ``ansible/**`` and is *not* what bare ``fnmatch``
    does. Absolute paths and paths escaping the repo root are always denied.
    """
    p = Path(rel_path)
    if p.is_absolute():
        return False
    parts = p.parts
    if not parts or ".." in parts:
        return False

    posix = p.as_posix()
    for glob in globs if globs is not None else allowed_paths():
        g = glob.strip()
        if not g:
            continue
        if fnmatch.fnmatch(posix, g):
            return True
        # Treat "dir/**" as also matching "dir/file" (fnmatch needs dir/*).
        if g.endswith("/**"):
            prefix = g[:-3]
            if posix == prefix or posix.startswith(prefix + "/"):
                return True
    return False


def resolve_in_repo(rel_path: str | Path) -> Path:
    """Resolve ``rel_path`` under the repo root, refusing traversal escapes."""
    root = repo_root()
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as e:
        raise ConfigError(
            f"path escapes the repository root: {rel_path}"
        ) from e
    return candidate
