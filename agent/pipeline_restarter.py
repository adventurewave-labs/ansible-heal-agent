"""Pipeline restarter — re-run the pipeline after a patch."""
from __future__ import annotations

from agent.config import repo_root
from pipeline import runner


def restart(playbook: str | None = None, run_id: str | None = None) -> runner.RunResult:
    """Re-run the pipeline. Returns the RunResult of the fresh run."""
    pb_path = None
    if playbook:
        pb_path = repo_root() / playbook
    return runner.run_pipeline(pb_path, run_id=run_id)
