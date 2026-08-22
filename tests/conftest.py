"""Shared test fixtures.

Every test that touches the filesystem or git runs against a throwaway repo
under pytest's ``tmp_path``. Nothing in the suite may write to, or commit to,
the developer's checkout — that was the old behaviour and it added 73 junk
commits to this repository's history before it was caught.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent import config

#: Captured at import time so tests that monkeypatch ``subprocess.run``
#: (e.g. the LLM z-ai backend tests) cannot break the isolation guard.
_REAL_RUN = subprocess.run


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return _REAL_RUN(["git", *args], cwd=repo, capture_output=True,
                     text=True, check=False)


@pytest.fixture
def scratch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An initialised git repo seeded with the broken Ansible baseline.

    The agent is pointed at it for the duration of the test via
    ``config.set_repo_root``. Environment knobs that would otherwise leak in
    from the developer's shell are cleared.
    """
    repo = tmp_path / "iac"
    repo.mkdir()

    for var in ("ANSIBLE_HEAL_REPO_ROOT", "ANSIBLE_HEAL_ALLOWED_PATHS",
                "ANSIBLE_HEAL_LLM_PROVIDER", "ANSIBLE_HEAL_LLM_MODEL",
                "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "PIPELINE_RUNNER"):
        monkeypatch.delenv(var, raising=False)

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    _git(repo, "config", "commit.gpgsign", "false")

    monkeypatch.setattr(config, "_OVERRIDE", repo.resolve())

    from scenarios.seed import reset_to_baseline
    reset_to_baseline()

    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "chore: baseline")

    return repo.resolve()


@pytest.fixture
def git_log_count(scratch_repo: Path):
    """Callable returning the current commit count in the scratch repo."""
    def _count() -> int:
        out = _git(scratch_repo, "rev-list", "--count", "HEAD").stdout.strip()
        return int(out or 0)
    return _count


@pytest.fixture(autouse=True)
def _guard_developer_checkout(request, monkeypatch: pytest.MonkeyPatch):
    """Fail loudly if a test forgets to isolate itself.

    Tests that do not request ``scratch_repo`` get the agent's own checkout as
    the repo root; that is fine for pure-function tests but must never be
    written to. We snapshot the working tree's git status and assert it is
    unchanged afterwards.
    """
    if "scratch_repo" in request.fixturenames:
        yield
        return

    own = Path(__file__).resolve().parent.parent
    if not (own / ".git").is_dir():
        yield
        return

    before = _git(own, "status", "--porcelain").stdout
    before_head = _git(own, "rev-parse", "HEAD").stdout
    yield
    after = _git(own, "status", "--porcelain").stdout
    after_head = _git(own, "rev-parse", "HEAD").stdout
    assert before == after, (
        f"test {request.node.name} mutated the developer's working tree; "
        "use the scratch_repo fixture"
    )
    assert before_head == after_head, (
        f"test {request.node.name} committed to the developer's checkout; "
        "use the scratch_repo fixture"
    )
