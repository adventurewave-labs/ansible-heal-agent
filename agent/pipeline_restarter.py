"""Pipeline restarter — re-run the pipeline after a patch."""
from __future__ import annotations

from agent import telemetry
from agent.config import repo_root
from pipeline import runner


@telemetry.traced(
    "ansible_heal.pipeline.run",
    attrs=lambda playbook=None, run_id=None: {
        "ansible_heal.playbook": playbook, "ansible_heal.run_id": run_id,
    },
    result_attrs=lambda r: {
        "process.exit.code": r.exit_code,
        "ansible_heal.failures": len(r.failures),
    },
)
def restart(playbook: str | None = None, run_id: str | None = None) -> runner.RunResult:
    """Re-run the pipeline. Returns the RunResult of the fresh run."""
    pb_path = None
    if playbook:
        pb_path = repo_root() / playbook
    return runner.run_pipeline(pb_path, run_id=run_id)
