"""CLI entrypoint — `python -m agent.cli` or `make demo` (which calls demo.py)."""
from __future__ import annotations

import time

import click

from agent.config import repo_root, transcripts_dir
from agent.core import Transcript, heal


@click.group()
@click.version_option("0.1.0")
def cli():
    """ansible-heal-agent — autonomous Ansible pipeline healer."""


@cli.command()
@click.option("--playbook", default="ansible/playbooks/site.yml", show_default=True,
              help="Playbook to run.")
@click.option("--max-retries", default=3, show_default=True, type=int,
              help="Max number of heal iterations.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Disable LLM; use deterministic fallback diagnoser only.")
@click.option("--transcript/--no-transcript", default=True,
              help="Write a Markdown transcript under transcripts/.")
def run(playbook: str, max_retries: int, no_llm: bool, transcript: bool):
    """Run the heal loop once and report the result."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    t_path = transcripts_dir() / f"demo-{ts}.md" if transcript else None
    t = Transcript(t_path, use_llm=not no_llm) if t_path else None

    result = heal(playbook=playbook, max_retries=max_retries,
                  use_llm=not no_llm, transcript=t)

    if t:
        t.footer(result)
        path = t.save()
        click.echo(f"\nTranscript: {path}")

    click.echo(f"Success: {result.success}  Iterations: {result.iterations}  "
               f"Final exit: {result.final_exit_code}")
    raise SystemExit(0 if result.success else 2)


@cli.command()
@click.option("--reset/--no-reset", default=True,
              help="Reset the repo to the broken baseline before running.")
def demo(reset: bool):
    """Run the end-to-end demo (reset → heal → transcript)."""
    if reset:
        from scenarios import seed
        seed.reset_to_baseline()
        click.echo("Repo reset to broken baseline.")

    ctx = click.get_current_context()
    ctx.invoke(run, playbook="ansible/playbooks/site.yml",
               max_retries=3, no_llm=False, transcript=True)


@cli.command()
def status():
    """Show the current repo state (git log + last pipeline run)."""
    from pipeline import git_helper
    click.echo("=== Recent commits ===")
    click.echo(git_helper.log(10))
    click.echo("\n=== Latest pipeline run (if any) ===")
    runs_dir = repo_root() / "pipeline" / "runs"
    if runs_dir.exists():
        logs = sorted(runs_dir.glob("run-*.log"))
        if logs:
            click.echo(f"Latest log: {logs[-1].relative_to(repo_root())}")
            click.echo(logs[-1].read_text()[:2000])
        else:
            click.echo("no runs yet")
    else:
        click.echo("no runs dir yet")


def main():
    cli()


if __name__ == "__main__":
    main()
