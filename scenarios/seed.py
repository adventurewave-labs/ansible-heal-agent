"""Seed / reset the demo baseline.

The "broken" baseline is shipped in-tree (see ansible/inventory.yml etc.).
``reset_to_baseline()`` reverts any agent-applied patches so each demo run
starts from the same broken state — making the demo reproducible.

It uses ``git checkout`` on the affected files. If the repo has no commits yet
(first run), it inits the repo with a baseline commit instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline.git_helper import REPO_ROOT, init_if_needed

# Files that the agent is allowed to modify — resetting these gives us a clean
# broken baseline.
PATCHABLE = [
    "ansible/inventory.yml",
    "ansible/group_vars/all.yml",
    "ansible/playbooks/webservers.yml",
    "ansible/playbooks/db.yml",
    "ansible/playbooks/site.yml",
]


def reset_to_baseline() -> None:
    """Revert patchable files to their committed baseline."""
    init_if_needed()
    # If the working tree has commits, restore baseline by checking out.
    res = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if res.returncode != 0:
        # First-run: stage everything and create the baseline commit.
        subprocess.run(["git", "add", "."], cwd=REPO_ROOT, check=True)
        subprocess.run(
            ["git", "commit", "-m", "chore: initial broken baseline"],
            cwd=REPO_ROOT, check=True,
        )
        return

    # Restore the patchable files to their committed state.
    for rel in PATCHABLE:
        subprocess.run(["git", "checkout", "--", rel], cwd=REPO_ROOT, check=False)


if __name__ == "__main__":
    import click

    @click.command()
    @click.option("--reset/--no-reset", default=True)
    def main(reset):
        if reset:
            reset_to_baseline()
            click.echo("Reset to broken baseline.")
        else:
            click.echo("No-op.")

    main()
