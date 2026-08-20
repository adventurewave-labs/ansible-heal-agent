"""Pipeline restarter — re-run the pipeline after a patch."""
from __future__ import annotations

from typing import Optional

from pipeline import runner
from pipeline.git_helper import REPO_ROOT


def restart(playbook: Optional[str] = None, run_id: Optional[str] = None) -> runner.RunResult:
    """Re-run the pipeline. Returns the RunResult of the fresh run."""
    pb_path = None
    if playbook:
        pb_path = REPO_ROOT / playbook
    return runner.run_pipeline(pb_path, run_id=run_id)
