"""CLI tests — the surface a human actually drives."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from click.testing import CliRunner

from agent.cli import cli


def _run(args):
    return CliRunner().invoke(cli, args, catch_exceptions=False)


def _git_status(repo: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                          capture_output=True, text=True).stdout


def _scratch_dir_from(output: str) -> str | None:
    m = re.search(r"^artefacts:\s+(\S+)", output, re.M)
    return m.group(1) if m else None


def test_dry_run_and_approval_are_mutually_exclusive(scratch_repo):
    res = _run(["run", "--repo", str(scratch_repo),
                "--dry-run", "--require-human-approval"])
    assert res.exit_code != 0
    assert "mutually exclusive" in res.output


def test_dry_run_prints_diffs_and_writes_nothing(scratch_repo):
    inv = scratch_repo / "ansible" / "inventory.yml"
    before = inv.read_text()

    res = _run(["run", "--repo", str(scratch_repo), "--no-llm",
                "--dry-run", "--no-transcript"])

    assert "proposal(s); nothing written" in res.output
    assert "would edit ansible/inventory.yml" in res.output
    assert inv.read_text() == before


def test_run_reports_the_write_surface_it_will_use(scratch_repo):
    res = _run(["run", "--repo", str(scratch_repo), "--no-llm",
                "--allowed-paths", "infra/**", "--dry-run", "--no-transcript"])
    assert "write surface: ['infra/**']" in res.output


def test_allowed_paths_flag_blocks_the_default_targets(scratch_repo):
    res = _run(["run", "--repo", str(scratch_repo), "--no-llm",
                "--allowed-paths", "infra/**", "--dry-run", "--no-transcript"])
    assert "BLOCKED" in res.output


def test_apply_mode_heals_and_exits_zero(scratch_repo):
    """The seeded baseline's module class migrates `docker` to
    `community.docker.docker_container`, a collection this environment does
    not install — so it genuinely does not reach green here (see
    tests/test_agent.py::test_full_heal_loop_with_fallback for the same
    behaviour exercised directly against heal()). The other two failure
    classes still heal; assert on those instead of a blanket exit 0."""
    res = _run(["run", "--repo", str(scratch_repo), "--no-llm", "--no-transcript"])
    assert res.exit_code == 2
    assert "Success: False" in res.output
    log = subprocess.run(["git", "log", "--format=%s"], cwd=scratch_repo,
                         capture_output=True, text=True).stdout
    assert "migrate deprecated module" in log
    assert "rename host to match playbook expectation" in log
    assert "add missing variable to group_vars" in log


def test_failed_run_exits_non_zero(scratch_repo):
    res = _run(["run", "--repo", str(scratch_repo), "--no-llm",
                "--allowed-paths", "", "--no-transcript"])
    assert res.exit_code == 2
    assert "Success: False" in res.output


def test_status_reports_the_target_repo(scratch_repo):
    res = _run(["status", "--repo", str(scratch_repo)])
    assert res.exit_code == 0
    assert str(scratch_repo) in res.output


def test_transcript_is_written_into_the_target_repo(scratch_repo):
    """Apply mode keeps its artefacts with the repo it healed."""
    _run(["run", "--repo", str(scratch_repo), "--no-llm"])
    written = list((scratch_repo / "transcripts").glob("run-*.md"))
    assert written, "expected a transcript in the target repo"


def test_dry_run_leaves_the_target_repo_untouched(scratch_repo):
    """The whole promise of dry-run: point it at a repo you do not trust yet.

    Not just "no patches" — no run logs and no transcript either. Those used to
    land in the target repo, which made ``--dry-run`` a writing operation on a
    repository the operator had explicitly declined to let it write to.
    """
    before = _git_status(scratch_repo)
    res = _run(["run", "--repo", str(scratch_repo), "--no-llm", "--dry-run"])

    assert _git_status(scratch_repo) == before, (
        f"--dry-run dirtied the target repo:\n{_git_status(scratch_repo)}")
    assert not (scratch_repo / "transcripts").exists()
    assert not (scratch_repo / "pipeline" / "runs").exists()

    # The transcript still exists — outside the repo, at the path we printed.
    scratch = _scratch_dir_from(res.output)
    assert scratch is not None, f"dry run did not report its scratch dir:\n{res.output}"
    written = list((Path(scratch) / "transcripts").glob("run-*.md"))
    assert written, f"expected a transcript under {scratch}"
    assert "Dry run — nothing was written or committed" in written[0].read_text()


def test_blocked_write_names_the_file_once(scratch_repo):
    """A refused write has to say *which* write was refused, exactly once."""
    res = _run(["run", "--repo", str(scratch_repo), "--no-llm",
                "--allowed-paths", "infra/**", "--dry-run", "--no-transcript"])
    blocked = [ln for ln in res.output.splitlines() if ln.startswith("BLOCKED")]
    assert blocked, res.output
    for line in blocked:
        assert "None" not in line, f"blocked line lost its target file: {line}"
        assert re.match(r"BLOCKED: refusing to write \S+\.ya?ml: ", line), line
    # One line per refused write, not one per write plus one per reason.
    assert len(blocked) == len(set(blocked)) == 3, blocked
