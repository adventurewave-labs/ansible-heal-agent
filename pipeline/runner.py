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

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agent.config import repo_root, runs_dir


@dataclass
class RunResult:
    exit_code: int
    log_path: str
    failures: list[dict] = field(default_factory=list)
    succeeded_hosts: list[str] = field(default_factory=list)


def _load_yaml(path: Path) -> dict:
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _expand_playbook(playbook_path: Path) -> list[dict]:
    """Walk a playbook file and expand ``import_playbook`` directives.

    Returns a flat list of play dicts (each one already has ``hosts:`` etc.).
    Each returned play is annotated with its source playbook so failures can be
    attributed to the right file.
    """
    # Resolve to absolute so relative_to(repo_root()) works regardless of cwd.
    playbook_path = playbook_path.resolve()
    raw = _load_yaml(playbook_path)
    if not isinstance(raw, list):
        raw = [raw]

    expanded: list[dict] = []
    for entry in raw:
        if isinstance(entry, dict) and "import_playbook" in entry:
            sub_path = playbook_path.parent / entry["import_playbook"]
            for sub in _expand_playbook(sub_path):
                expanded.append(sub)
        elif isinstance(entry, dict):
            entry["_source_playbook"] = str(playbook_path.relative_to(repo_root()))
            expanded.append(entry)
    return expanded


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


def _check_removed_modules(task: dict) -> str | None:
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


def run(playbook_path: Path, run_id: str | None = None) -> RunResult:
    """Execute the mock playbook and return a RunResult.

    Two-phase execution (mirrors real ansible-playbook):
      Phase A — parse-time validation:
        Walk all plays + tasks. Emit failures for removed modules and
        undefined-variable refs that can be detected statically.
      Phase B — runtime execution:
        For each play, resolve its host pattern against the inventory. If the
        pattern matches no host, emit an UNREACHABLE failure. Otherwise, run
        each task; if no parse-time failure was recorded for it, mark its hosts
        as succeeded.
    """
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
    log_path = runs_dir() / f"run-{run_id}.log"
    failures: list[dict] = []
    succeeded: list[str] = []

    # Resolve playbook_path to absolute so relative_to(repo_root()) works from any cwd.
    playbook_path = playbook_path.resolve()
    inv = _load_yaml(repo_root() / "ansible" / "inventory.yml")
    group_vars = _load_yaml(repo_root() / "ansible" / "group_vars" / "all.yml") or {}
    playbook = _expand_playbook(playbook_path)

    log_lines: list[str] = []
    log_lines.append(f"PLAYBOOK: {playbook_path.name} ***********************************")
    log_lines.append(f"PLAY [configure stack] : started at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log_lines.append("")
    log_lines.append("PHASE A: Parse-time validation")
    log_lines.append("")

    rc = 0

    # ── Phase A: parse-time validation ─────────────────────────────────────
    parse_failures_by_task: dict[tuple[str, str], list[dict]] = {}
    for play in playbook:
        target = play.get("hosts", "all")
        play_name = play.get("name", "<unnamed play>")
        play_source = play.get("_source_playbook", str(playbook_path.relative_to(repo_root())))
        tasks = play.get("tasks", [])

        for task in tasks:
            task_name = task.get("name", "<unnamed task>")
            key = (play_source, task_name)

            # Removed-module check
            removed_err = _check_removed_modules(task)
            if removed_err:
                rc = 2
                f = {
                    "type": "removed_module",
                    "host": target,
                    "module": next(
                        (k for k in task if k.split(".")[-1] in {"apt_key", "docker"}),
                        "unknown"
                    ),
                    "message": removed_err,
                    "playbook": play_source,
                    "play": play_name,
                    "task": task_name,
                }
                failures.append(f)
                parse_failures_by_task.setdefault(key, []).append(f)
                log_lines.append(
                    f"ERROR! couldn't resolve module action '{f['module']}'. "
                    f"The module was removed in ansible-core 2.18."
                )
                continue

            # Undefined-var check (template vars)
            refs = _collect_template_vars(task)
            undefined = [r for r in refs if r not in group_vars]
            if undefined:
                rc = 2
                var = undefined[0]
                f = {
                    "type": "undefined_variable",
                    "host": target,
                    "variable": var,
                    "message": (
                        f"The task includes an option with an undefined variable '{var}'. "
                        f"The error was: 'dict object' has no attribute '{var}'"
                    ),
                    "playbook": play_source,
                    "play": play_name,
                    "task": task_name,
                }
                failures.append(f)
                parse_failures_by_task.setdefault(key, []).append(f)
                log_lines.append(
                    f"ERROR! undefined variable '{var}' referenced in task '{task_name}'."
                )

    log_lines.append("")
    log_lines.append("PHASE B: Runtime execution")
    log_lines.append("")

    # ── Phase B: runtime execution ───────────────────────────────────────
    for play in playbook:
        target = play.get("hosts", "all")
        play_name = play.get("name", "<unnamed play>")
        play_source = play.get("_source_playbook", str(playbook_path.relative_to(repo_root())))
        tasks = play.get("tasks", [])

        hosts = _hosts_in_inventory(inv, target)

        log_lines.append(f"PLAY [{play_name}] *********************************************")
        log_lines.append(f"TASK [target hosts pattern '{target}'] resolved to {len(hosts)} host(s)")

        if not hosts:
            # Hostname doesn't exist in inventory — emit UNREACHABLE
            rc = 2
            fake_host = target if not target.startswith(("all", "*")) else "<unknown>"
            failures.append({
                "type": "unreachable_host",
                "host": fake_host,
                "pattern": target,
                "message": f"UNREACHABLE! fatal: [{fake_host}]: UNREACHABLE! "
                           f"Host '{fake_host}' not found in inventory.",
                "playbook": play_source,
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
            key = (play_source, task_name)
            if key in parse_failures_by_task:
                for f in parse_failures_by_task[key]:
                    log_lines.append(f"fatal: [{f.get('host', '?')}]: FAILED! => {f['message']}")
                continue

            # Success path
            for h in hosts:
                if h not in succeeded:
                    succeeded.append(h)
                log_lines.append(f"ok: [{h}]")
            log_lines.append("")

        log_lines.append(
            "PLAY RECAP: " + "  ".join(
                f"{h} : ok=1  changed=0  unreachable=0  failed=0" for h in hosts
            )
        )
        log_lines.append("")

    log_lines.append(f"EXIT CODE: {rc}")
    log_path.write_text("\n".join(log_lines) + "\n")

    # Structured sidecar, so consumers never have to parse the mock's prose.
    # The real runner's callback plugin writes the same shape.
    log_path.with_suffix(".json").write_text(json.dumps(failures, indent=2))

    return RunResult(
        exit_code=rc,
        log_path=str(log_path.relative_to(repo_root())),
        failures=failures,
        succeeded_hosts=succeeded,
    )


#: Directory holding the Ansible callback plugin that emits structured failures.
CALLBACK_DIR = Path(__file__).resolve().parent / "callback_plugins"


class RunnerUnavailable(RuntimeError):
    """Raised when the requested runner cannot be used."""


def _merge_failures(primary: list[dict], extra: list[dict]) -> list[dict]:
    """Merge two failure lists, dropping duplicates.

    The callback sidecar and the text scan overlap for runtime failures; they
    must not both be reported. Identity is (type, host, variable-or-module).
    """
    def key(f: dict) -> tuple:
        return (
            f.get("type"),
            f.get("host"),
            f.get("variable") or f.get("module_short") or f.get("module"),
        )

    seen = {key(f) for f in primary}
    merged = list(primary)
    for f in extra:
        if key(f) not in seen:
            seen.add(key(f))
            merged.append(f)
    return merged


def run_real(playbook_path: Path, run_id: str | None = None) -> RunResult:
    """Run the real ``ansible-playbook`` and return structured failures.

    Failures come from two places, because no single source sees everything:

    * The ``heal_json`` callback plugin, for runtime events (task failures,
      unreachable hosts, and a host pattern matching nothing — which real
      Ansible reports as a *warning* while exiting 0).
    * A text scan of stdout/stderr, for parse-time errors. An unresolvable
      module aborts before any callback fires and exits 4, so the callback
      never sees it.

    Exit-code handling matches what the pipeline actually means rather than
    what Ansible returns: a run that skipped a whole play because its host
    pattern matched nothing exits 0, and is reported here as a failure.
    """
    if not shutil.which("ansible-playbook"):
        raise RunnerUnavailable(
            "PIPELINE_RUNNER=real but ansible-playbook is not on PATH. "
            "Install ansible-core, or unset PIPELINE_RUNNER to use the mock."
        )

    run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
    log_path = runs_dir() / f"run-{run_id}.log"
    sidecar = log_path.with_suffix(".json")
    if sidecar.exists():
        sidecar.unlink()

    inventory = repo_root() / "ansible" / "inventory.yml"
    env = dict(os.environ)
    env.update({
        "ANSIBLE_CALLBACK_PLUGINS": str(CALLBACK_DIR),
        "ANSIBLE_CALLBACKS_ENABLED": "heal_json",
        "ANSIBLE_HEAL_SIDECAR": str(sidecar),
        "ANSIBLE_FORCE_COLOR": "0",
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
    })

    proc = subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(playbook_path)],
        capture_output=True, text=True, check=False,
        cwd=str(repo_root()), env=env,
    )
    log_path.write_text(proc.stdout + proc.stderr)

    from agent import log_scanner

    payload = log_scanner.read_sidecar(log_path)
    failures = list(payload) if payload else []
    failures = _merge_failures(
        failures, log_scanner.extract_failures(log_path, text_only=True))

    ok_hosts: list[str] = []
    if sidecar.exists():
        try:
            ok_hosts = json.loads(sidecar.read_text()).get("ok_hosts", [])
        except (json.JSONDecodeError, AttributeError):
            ok_hosts = []

    rc = proc.returncode
    if rc == 0 and failures:
        # A play skipped because its host pattern matched nothing is not a
        # healthy pipeline, whatever Ansible's exit code says.
        rc = 2
    if rc != 0 and not failures:
        # Never report "it failed but there is nothing to fix" — that is how
        # the previous implementation silently did nothing for three rounds.
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
        failures = [{
            "type": "unclassified",
            "host": None,
            "message": " ".join(tail)[:500] or f"ansible-playbook exited {rc}",
        }]

    return RunResult(
        exit_code=rc,
        log_path=str(log_path.relative_to(repo_root())),
        failures=failures,
        succeeded_hosts=ok_hosts,
    )


def run_pipeline(playbook_path: Path | None = None, run_id: str | None = None) -> RunResult:
    """Public entrypoint — picks the mock or real runner from PIPELINE_RUNNER.

    ``PIPELINE_RUNNER=real`` with no ansible-playbook installed raises rather
    than silently running the mock: an operator who asked for the real thing
    and got a simulation would have no way to tell.
    """
    default_pb = repo_root() / "ansible" / "playbooks" / "site.yml"
    if os.environ.get("PIPELINE_RUNNER", "mock") == "real":
        return run_real(playbook_path or default_pb, run_id)
    return run(playbook_path or default_pb, run_id)


if __name__ == "__main__":
    _default = repo_root() / "ansible" / "playbooks" / "site.yml"
    pb = Path(sys.argv[1]) if len(sys.argv) > 1 else _default
    result = run_pipeline(pb)
    print(f"\nexit_code={result.exit_code}")
    print(f"log_path={result.log_path}")
    print(f"failures={len(result.failures)}")
    sys.exit(result.exit_code)
