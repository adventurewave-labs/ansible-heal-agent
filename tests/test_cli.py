"""CLI tests — the surface a human actually drives."""

from __future__ import annotations

from click.testing import CliRunner

from agent.cli import cli


def _run(args):
    return CliRunner().invoke(cli, args, catch_exceptions=False)


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
    res = _run(["run", "--repo", str(scratch_repo), "--no-llm", "--no-transcript"])
    assert res.exit_code == 0
    assert "Success: True" in res.output


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
    _run(["run", "--repo", str(scratch_repo), "--no-llm", "--dry-run"])
    written = list((scratch_repo / "transcripts").glob("run-*.md"))
    assert written, "expected a transcript in the target repo"
    assert "Dry run — nothing was written or committed" in written[0].read_text()
