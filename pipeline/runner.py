"""Mock ansible-playbook runner.

Parses the in-repo playbooks and inventory, walks the tasks, and emits log
lines that mirror real Ansible output for three well-known failure classes:

  1. Stale hostname       → "UNREACHABLE! fatal: [web-server-01]: UNREACHABLE! ..."
  2. Removed module       → "ERROR! couldn't resolve module action 'apt_key' ..."
  3. Undefined variable   → "fatal: [web-01]: FAILED! => msg=The task includes an
                              option with an undefined variable 'nginx_port'."

Exit codes mirror real Ansible:
  0  → success
  2  → task failure (one or more hosts failed)
  1  → playbook parse error

The runner writes a timestamped log to ``pipeline/runs/run-<ts>.log`` and
also returns the log path so the agent can pick it up immediately.
"""

from __future__ import annotations

import os
import re
import sys
import time
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "pipeline" / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class RunResult:
    exit_code: int
    log_path: str
    failures: list[dict] = field(default_factory=list)
    succeeded_hosts: list[str] = field(default_factory=list)


def _load_yaml(path: Path) -> dict:
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _hosts_in_inventory(inv: dict, pattern: str) -> list[str]:
    """Return the list of concrete host names matching an Ansible hosts pattern.

    Supports: literal hostname, group name, 'all', and '*' wildcard.
    """
    # Build a flat {host: meta} map from the inventory tree.
    flat: dict[str, dict] = {}

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "hosts" and isinstance(v, dict):
                    flat.update(v)
                elif isinstance(v, dict):
                    walk(v)

    walk(inv.get("all", inv))

    # Pattern resolution
    if pattern == "all":
        return list(flat.keys())
    if pattern in flat:
        return [pattern]
    if "*" in pattern:
        regex = re.compile("^" + pattern.replace("*", ".*") + "$")
        return [h for h in flat if regex.match(h)]

    # Group lookup
    groups: dict[str, list[str]] = {}

    def collect_groups(node, prefix=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "hosts" and isinstance(v, dict):
                    return
                if k == "children" and isinstance(v, dict):
                    for gk, gv in v.items():
                        gh = []
                        if isinstance(gv, dict) and "hosts" in gv:
                            gh = list(gv["hosts"].keys())
                        groups[gk] = gh
                elif isinstance(v, dict):
                    collect_groups(v, prefix + k)

    collect_groups(inv.get("all", inv))
    if pattern in groups:
        return groups[pattern]

    return []  # pattern matched nothing → unreachable


def _check_removed_modules(task: dict) -> Optional[str]:
    """Return error string if the task uses a removed module."""
    REMOVED = {
        "apt_key": "The 'apt_key' module was removed in ansible-core 2.18. "
                   "Use ansible.builtin.get_url + ansible.builtin.command "
                   "to add apt keys instead.",
        "docker": "The 'docker' module was removed. Use community.docker.docker_container.",
    }
    for key in task:
        # strip ansible.builtin. prefix
        mod = key.split(".")[-1]
        if mod in REMOVED:
            return REMOVED[mod]
    return None


def _collect_template_vars(task: dict) -> list[str]:
    """Return list of undefined-var refs inside the task's template vars block."""
    refs = []
    tpl = task.get("template") or task.get("ansible.builtin.template")
    if isinstance(tpl, dict):
        raw = yaml.safe_dump(tpl.get("vars", {}))
        refs.extend(re.findall(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", raw))
    return refs


def run(playbook_path: Path, run_id: Optional[str] = None) -> RunResult:
    """Execute the mock playbook and return a RunResult."""
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
    log_path = RUNS_DIR / f"run-{run_id}.log"
    failures: list[dict] = []
    succeeded: list[str] = []

    inv = _load_yaml(REPO_ROOT / "ansible" / "inventory.yml")
    group_vars = _load_yaml(REPO_ROOT / "ansible" / "group_vars" / "all.yml")
    playbook = _load_yaml(playbook_path)

    log_lines: list[str] = []
    log_lines.append(f"PLAYBOOK: {playbook_path.name} ***********************************")
    log_lines.append(f"PLAY [configure stack] : started at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log_lines.append("")

    rc = 0

    for play in playbook:
        target = play.get("hosts", "all")
        play_name = play.get("name", "<unnamed play>")
        tasks = play.get("tasks", [])

        hosts = _hosts_in_inventory(inv, target)

        log_lines.append(f"PLAY [{play_name}] *********************************************")
        log_lines.append(f"TASK [target hosts pattern '{target}'] resolved to {len(hosts)} host(s)")

        if not hosts:
            # Hostname doesn't exist in inventory — emit Ansible's UNREACHABLE
            rc = 2
            fake_host = target if not target.startswith(("all", "*")) else "<unknown>"
            failures.append({
                "type": "unreachable_host",
                "host": fake_host,
                "pattern": target,
                "message": f"UNREACHABLE! fatal: [{fake_host}]: UNREACHABLE! "
                           f"Host '{fake_host}' not found in inventory.",
                "playbook": str(playbook_path.relative_to(REPO_ROOT)),
                "play": play_name,
            })
            log_lines.append(
                f"fatal: [{fake_host}]: UNREACHABLE! Host '{fake_host}' not found "
                f"in inventory. Pattern '{target}' matched 0 hosts."
            )
            log_lines.append(f"PLAY RECAP: {fake_host} : ok=0  changed=0  unreachable=1  failed=0")
            log_lines.append("")
            continue

        for task in tasks:
            task_name = task.get("name", "<unnamed task>")
            log_lines.append(f"TASK [{task_name}] *****************************************")

            # 1) Removed-module check
            removed_err = _check_removed_modules(task)
            if removed_err:
                rc = 2
                for h in hosts:
                    failures.append({
                        "type": "removed_module",
                        "host": h,
                        "module": next(
                            (k for k in task if k.split(".")[-1] in {"apt_key", "docker"}),
                            "unknown"
                        ),
                        "message": removed_err,
                        "playbook": str(playbook_path.relative_to(REPO_ROOT)),
                        "play": play_name,
                        "task": task_name,
                    })
                    log_lines.append(
                        f"fatal: [{h}]: FAILED! => {removed_err}"
                    )
                log_lines.append(
                    "ERROR! couldn't resolve module action 'apt_key'. "
                    "The module was removed in ansible-core 2.18."
                )
                continue

            # 2) Undefined-var check (template vars)
            refs = _collect_template_vars(task)
            undefined = [r for r in refs if r not in (group_vars or {})]
            if undefined:
                rc = 2
                for h in hosts:
                    var = undefined[0]
                    failures.append({
                        "type": "undefined_variable",
                        "host": h,
                        "variable": var,
                        "message": (
                            f"The task includes an option with an undefined variable '{var}'. "
                            f"The error was: 'dict object' has no attribute '{var}'"
                        ),
                        "playbook": str(playbook_path.relative_to(REPO_ROOT)),
                        "play": play_name,
                        "task": task_name,
                    })
                    log_lines.append(
                        f"fatal: [{h}]: FAILED! => msg=\"The task includes an option "
                        f"with an undefined variable '{var}'. "
                        f"The error was: 'dict object' has no attribute '{var}'\""
                    )
                continue

            # 3) Success path
            for h in hosts:
                if h not in succeeded:
                    succeeded.append(h)
                log_lines.append(f"ok: [{h}]")
            log_lines.append("")

        log_lines.append(
            f"PLAY RECAP: " + "  ".join(
                f"{h} : ok={len([f for f in failures if f['host'] != h])}  "
                f"changed=0  unreachable=0  failed={len([f for f in failures if f['host'] == h])}"
                for h in hosts
            )
        )
        log_lines.append("")

    log_lines.append(f"EXIT CODE: {rc}")
    log_path.write_text("\n".join(log_lines) + "\n")

    return RunResult(
        exit_code=rc,
        log_path=str(log_path.relative_to(REPO_ROOT)),
        failures=failures,
        succeeded_hosts=succeeded,
    )


def run_real(playbook_path: Path, run_id: Optional[str] = None) -> RunResult:
    """Run real ansible-playbook if available. Used when PIPELINE_RUNNER=real."""
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
    log_path = RUNS_DIR / f"run-{run_id}.log"
    cmd = [
        "ansible-playbook",
        "-i", str(REPO_ROOT / "ansible" / "inventory.yml"),
        str(playbook_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    log_path.write_text(proc.stdout + proc.stderr)
    return RunResult(
        exit_code=proc.returncode,
        log_path=str(log_path.relative_to(REPO_ROOT)),
        failures=[],
        succeeded_hosts=[],
    )


def run_pipeline(playbook_path: Optional[Path] = None, run_id: Optional[str] = None) -> RunResult:
    """Public entrypoint — picks mock or real runner based on env var."""
    use_real = os.environ.get("PIPELINE_RUNNER", "mock") == "real"
    if use_real and shutil.which("ansible-playbook"):
        return run_real(playbook_path or (REPO_ROOT / "ansible" / "playbooks" / "site.yml"), run_id)
    return run(playbook_path or (REPO_ROOT / "ansible" / "playbooks" / "site.yml"), run_id)


if __name__ == "__main__":
    pb = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "ansible" / "playbooks" / "site.yml"
    result = run_pipeline(pb)
    print(f"\nexit_code={result.exit_code}")
    print(f"log_path={result.log_path}")
    print(f"failures={len(result.failures)}")
    sys.exit(result.exit_code)
