"""CLI entrypoint — ``ansible-heal`` or ``python -m agent.cli``."""
from __future__ import annotations

import time

import click

from agent import config
from agent.core import MODE_APPLY, MODE_DRY_RUN, MODE_PR, Transcript, heal


def _apply_common(repo: str | None, allowed_paths: str | None) -> None:
    if repo:
        config.set_repo_root(repo)
    if allowed_paths is not None:
        import os
        os.environ["ANSIBLE_HEAL_ALLOWED_PATHS"] = allowed_paths


@click.group()
@click.version_option("0.1.0")
def cli():
    """ansible-heal-agent — autonomous Ansible pipeline healer."""


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
              help="Diagnose and show the diffs that would be applied. "
                   "Writes nothing, commits nothing.")
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
def run(repo, playbook, max_retries, allowed_paths, dry_run,
        require_human_approval, remote, no_llm, transcript):
    """Run the heal loop once and report the result."""
    if dry_run and require_human_approval:
        raise click.UsageError(
            "--dry-run and --require-human-approval are mutually exclusive: "
            "one writes nothing at all, the other writes to a branch.")

    _apply_common(repo, allowed_paths)
    mode = MODE_DRY_RUN if dry_run else MODE_PR if require_human_approval else MODE_APPLY

    ts = time.strftime("%Y%m%d-%H%M%S")
    t_path = config.transcripts_dir() / f"run-{ts}.md" if transcript else None
    t = Transcript(t_path, use_llm=not no_llm) if t_path else None

    click.echo(f"repo:  {config.repo_root()}")
    click.echo(f"mode:  {mode}")
    click.echo(f"write surface: {list(config.allowed_paths())}")
    click.echo("")

    result = heal(playbook=playbook, max_retries=max_retries,
                  use_llm=not no_llm, transcript=t, mode=mode, remote=remote)

    if mode == MODE_DRY_RUN:
        click.echo(f"{len(result.proposals)} proposal(s); nothing written.")
        for p in result.proposals:
            click.echo("")
            if p.blocked_reason:
                click.echo(f"BLOCKED {p.target_file}: {p.blocked_reason}")
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

    for reason in result.blocked:
        click.echo(f"BLOCKED: {reason}", err=True)

    if t:
        t.footer(result)
        click.echo(f"\ntranscript: {t.save()}")

    click.echo(f"\nSuccess: {result.success}  Iterations: {result.iterations}  "
               f"Final exit: {result.final_exit_code}")
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


def main():
    cli()


if __name__ == "__main__":
    main()
