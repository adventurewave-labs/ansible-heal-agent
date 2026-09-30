"""CLI entrypoint — ``ansible-heal`` or ``python -m agent.cli``."""
from __future__ import annotations

import tempfile
import time

import click

from agent import __version__, config
from agent.core import MODE_APPLY, MODE_DRY_RUN, MODE_PR, Transcript, heal


def _apply_common(repo: str | None, allowed_paths: str | None) -> None:
    if repo:
        try:
            config.set_repo_root(repo)
        except config.ConfigError as e:
            raise click.UsageError(str(e)) from e
    if allowed_paths is not None:
        import os
        os.environ["ANSIBLE_HEAL_ALLOWED_PATHS"] = allowed_paths


@click.group()
@click.version_option(__version__)
def cli():
    """ansible-heal-agent — autonomous Ansible pipeline healer."""
    # Ships spans over OTLP when OTEL_EXPORTER_OTLP_ENDPOINT is set and the
    # [otel] extra is installed; otherwise a no-op.
    from agent import telemetry
    telemetry.configure_from_env()


@cli.command()
@click.option("--repo", default=None,
              help="Path to the IaC repository to operate on "
                   "[default: ANSIBLE_HEAL_REPO_ROOT, else this checkout].")
@click.option("--playbook", default="ansible/playbooks/site.yml", show_default=True,
              help="Playbook to run, relative to the repo root.")
@click.option("--max-retries", default=3, show_default=True, type=int,
              help="Max heal iterations (apply mode only).")
@click.option("--allowed-paths", default=None,
              help="Comma-separated globs the patcher may write to "
                   "[default: ansible/**]. An empty value denies all writes.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Diagnose and show the diffs that would be applied. Writes "
                   "nothing into the target repository and commits nothing; the "
                   "run log and transcript go to a scratch directory outside it.")
@click.option("--require-human-approval", is_flag=True, default=False,
              help="Commit fixes to a new heal/<run-id> branch, push it and open "
                   "a PR, then stop. The base branch is not modified and the "
                   "pipeline is not re-run.")
@click.option("--remote", default="origin", show_default=True,
              help="Remote to push the heal branch to (approval mode).")
@click.option("--no-llm", is_flag=True, default=False,
              help="Disable the LLM; use the deterministic diagnoser only.")
@click.option("--transcript/--no-transcript", default=True,
              help="Write a Markdown transcript under transcripts/.")
@click.option("--sarif", "sarif_path", default=None, metavar="PATH",
              help="With --dry-run: also write findings as SARIF 2.1.0 to PATH "
                   "(for GitHub code scanning).")
@click.option("--fail-on-findings", is_flag=True, default=False,
              help="With --dry-run: exit 2 when any failure was found, even if "
                   "a SARIF file was written (default: exit 0 once reported).")
def run(repo, playbook, max_retries, allowed_paths, dry_run,
        require_human_approval, remote, no_llm, transcript, sarif_path,
        fail_on_findings):
    """Run the heal loop once and report the result."""
    if dry_run and require_human_approval:
        raise click.UsageError(
            "--dry-run and --require-human-approval are mutually exclusive: "
            "one writes nothing at all, the other writes to a branch.")

    if (sarif_path or fail_on_findings) and not dry_run:
        raise click.UsageError(
            "--sarif and --fail-on-findings report dry-run proposals; add --dry-run.")

    _apply_common(repo, allowed_paths)
    mode = MODE_DRY_RUN if dry_run else MODE_PR if require_human_approval else MODE_APPLY

    # A dry run must leave the target repository byte-for-byte untouched — that
    # is the entire point of pointing it at a repo you do not trust the agent
    # with yet. Run logs and transcripts are still useful, so they are written
    # outside the repo instead of being suppressed.
    scratch = tempfile.mkdtemp(prefix="ansible-heal-dryrun-") if mode == MODE_DRY_RUN else None
    config.set_output_root(scratch)

    ts = time.strftime("%Y%m%d-%H%M%S")
    t_path = config.transcripts_dir() / f"run-{ts}.md" if transcript else None
    t = Transcript(t_path, use_llm=not no_llm) if t_path else None

    click.echo(f"repo:  {config.repo_root()}")
    click.echo(f"mode:  {mode}")
    click.echo(f"write surface: {list(config.allowed_paths())}")
    if scratch:
        click.echo(f"artefacts:     {scratch}  (nothing is written to the repo)")
    click.echo("")

    result = heal(playbook=playbook, max_retries=max_retries,
                  use_llm=not no_llm, transcript=t, mode=mode, remote=remote)

    if mode == MODE_DRY_RUN:
        click.echo(f"{len(result.proposals)} proposal(s); nothing written.")
        for p in result.proposals:
            click.echo("")
            if p.blocked_reason:
                # The reason already names the file; prefixing target_file too
                # printed it twice (and printed "None" whenever the fix was
                # discarded before the proposal was built).
                click.echo(f"BLOCKED: {p.blocked_reason}")
                continue
            if not p.target_file:
                reason = p.diagnosis.get("_no_fix_reason") or p.diagnosis.get("diagnosis")
                click.echo(f"NO FIX: {reason}")
                continue
            click.echo(f"--- would edit {p.target_file} ---")
            click.echo(p.diff)
    elif mode == MODE_PR:
        click.echo(f"branch: {result.branch}")
        click.echo(f"pushed: {result.pushed}")
        if result.pr_url:
            click.echo(f"pull request: {result.pr_url}")
        if result.push_error:
            click.echo(f"note: {result.push_error}")

    if mode != MODE_DRY_RUN:
        # Dry-run already printed each block above, with its target file; this
        # is the path for the modes that do not enumerate proposals.
        for reason in result.blocked:
            click.echo(f"BLOCKED: {reason}", err=True)
        # A refusal is a result, not silence. Without this the operator saw a
        # run that simply did less than expected, with the reason buried in the
        # transcript JSON.
        for reason in result.declined:
            click.echo(f"NO FIX: {reason}", err=True)

    if t:
        t.footer(result)
        click.echo(f"\ntranscript: {t.save()}")

    click.echo(f"\nSuccess: {result.success}  Iterations: {result.iterations}  "
               f"Final exit: {result.final_exit_code}")
    if sarif_path:
        from agent import sarif
        out = sarif.write(result, sarif_path)
        click.echo(f"sarif: {out}  ({len(result.proposals)} result(s))")
        # Findings reported as SARIF are the output, not a crash: code
        # scanning needs the upload step to run. --fail-on-findings restores
        # a failing exit for pipelines that want to gate on them.
        found = bool(result.proposals)
        raise SystemExit(2 if (found and fail_on_findings) else 0)
    raise SystemExit(0 if result.success else 2)


@cli.command()
@click.option("--repo", default=None, help="Repository to inspect.")
def status(repo):
    """Show the target repo's state (git log + last pipeline run)."""
    _apply_common(repo, None)
    from pipeline import git_helper
    click.echo(f"repo: {config.repo_root()}")
    click.echo(f"write surface: {list(config.allowed_paths())}")
    click.echo("\n=== Recent commits ===")
    click.echo(git_helper.log(10))
    click.echo("\n=== Latest pipeline run (if any) ===")
    runs = config.repo_root() / "pipeline" / "runs"
    logs = sorted(runs.glob("run-*.log")) if runs.exists() else []
    if logs:
        click.echo(f"Latest log: {logs[-1].relative_to(config.repo_root())}")
        click.echo(logs[-1].read_text()[:2000])
    else:
        click.echo("no runs yet")


@cli.command(name="eval")
@click.option("--out", "out_dir", default="eval-report", show_default=True,
              help="Directory for eval-report.json and eval-report.md.")
@click.option("--runner", type=click.Choice(["mock", "real"]), default="mock",
              show_default=True, help="Pipeline runner (real = ansible-playbook).")
@click.option("--llm/--no-llm", "use_llm", default=False, show_default=True,
              help="Score the LLM path instead of the deterministic one.")
@click.option("--filter", "name_filter", default=None,
              help="Only run cases whose name contains this substring.")
@click.option("--min-heal-rate", type=float, default=None,
              help="Exit non-zero if the heal rate falls below this (0-1).")
@click.option("--max-false-fix-rate", type=float, default=None,
              help="Exit non-zero if the false-fix rate exceeds this (0-1).")
def eval_cmd(out_dir, runner, use_llm, name_filter, min_heal_rate,
             max_false_fix_rate):
    """Measure heal rate and false-fix rate over a generated corpus.

    Every case runs in its own throwaway git repo; nothing here touches the
    repository you run it from.
    """
    import os
    from pathlib import Path

    from agent import evaluation

    os.environ["PIPELINE_RUNNER"] = runner
    cases = evaluation.build_corpus()
    if name_filter:
        cases = [c for c in cases if name_filter in c.name]
    if not cases:
        raise click.UsageError("no corpus case matches --filter")

    def progress(r):
        mark = "ok  " if r.correct else "FAIL"
        click.echo(f"{mark} {r.outcome:<9} {r.name}  ({r.seconds}s)")

    report = evaluation.run_eval(cases, use_llm=use_llm, progress=progress)
    j, m = evaluation.write_report(report, Path(out_dir))
    s = report["summary"]
    click.echo("")
    click.echo(f"heal rate: {evaluation._pct(s['heal_rate'])}   "
               f"false-fix rate: {evaluation._pct(s['false_fix_rate'])}   "
               f"decline precision: {evaluation._pct(s['decline_precision'])}")
    click.echo(f"report: {j}  {m}")

    failed = []
    if min_heal_rate is not None and (s["heal_rate"] or 0) < min_heal_rate:
        failed.append(f"heal rate {s['heal_rate']} < {min_heal_rate}")
    if (max_false_fix_rate is not None and s["false_fix_rate"] is not None
            and s["false_fix_rate"] > max_false_fix_rate):
        failed.append(f"false-fix rate {s['false_fix_rate']} > {max_false_fix_rate}")
    for f in failed:
        click.echo(f"GATE FAILED: {f}", err=True)
    raise SystemExit(1 if failed else 0)


@cli.command(name="mcp")
def mcp_cmd():
    """Serve read-only diagnose/explain tools over MCP (stdio).

    Register with a client, e.g. `claude mcp add ansible-heal -- ansible-heal mcp`.
    """
    from agent import mcp_server
    mcp_server.serve()


def main():
    cli()


if __name__ == "__main__":
    main()
