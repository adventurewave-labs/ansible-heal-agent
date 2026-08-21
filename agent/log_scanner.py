"""Log scanner — extract structured failures from ansible-playbook output.

Two sources, in priority order:

1. **The structured sidecar.** ``pipeline/callback_plugins/heal_json.py`` (real
   runs) and ``pipeline/runner.py`` (mock runs) both write one next to the log.
   This is the authoritative source and needs no pattern matching.
2. **Regexes over the text**, for what the sidecar cannot contain: parse-time
   errors abort before any callback fires, so a removed or misspelled module
   (ansible-core exits **4**) only ever appears in stdout.

The patterns below match *real* ansible-core output, which the original ones
did not. Verified against ansible-core 2.19:

    [ERROR]: couldn't resolve module/action 'ansible.builtin.docker'.
    Error while resolving value for 'msg': 'nginx_port' is undefined
    [WARNING]: Could not match supplied host pattern, ignoring: web-server-01
    fatal: [db-01]: UNREACHABLE! => {"changed": false, "msg": "..."}

The mock runner's phrasings are kept alongside them so mock logs still parse.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# "couldn't resolve module/action 'x'" (real) or "module action 'x'" (mock).
MODULE_RE = re.compile(r"couldn't resolve module[ /]action '([^']+)'")

# Real ansible: "'nginx_port' is undefined". Mock: "undefined variable 'x'".
UNDEFVAR_REAL_RE = re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)' is undefined")
UNDEFVAR_MOCK_RE = re.compile(r"undefined variable '([^']+)'")

# Real ansible emits this as a WARNING and still exits 0.
NO_HOSTS_RE = re.compile(r"Could not match supplied host pattern, ignoring:\s*(\S+)")

# Real unreachable lines carry a JSON blob; the host is the reliable part.
UNREACHABLE_RE = re.compile(r"fatal:\s*\[([^\]]+)\]:\s*UNREACHABLE!")

# Mock-only phrasing, kept so mock logs keep parsing.
UNREACHABLE_MOCK_RE = re.compile(
    r"fatal:\s*\[([^\]]+)\]:\s*UNREACHABLE!.*Host '([^']+)' not found")


def short_module_name(name: str) -> str:
    """``ansible.builtin.apt_key`` -> ``apt_key``."""
    return name.rsplit(".", 1)[-1]


def read_sidecar(log_path: Path) -> list[dict[str, Any]] | None:
    """Return failures from the JSON sidecar beside ``log_path``, if present.

    Accepts both shapes in use: a bare list (mock runner) and the callback
    plugin's ``{"failures": [...], "ok_hosts": [...]}``.
    """
    sidecar = log_path.with_suffix(".json")
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text())
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("failures"), list):
        return data["failures"]
    return None


def extract_failures(log_path: str | Path,
                     text_only: bool = False) -> list[dict[str, Any]]:
    """Return failure dicts, ordered by appearance.

    ``text_only=True`` skips the sidecar and parses the text. The real runner
    uses it to pick up parse-time errors that no callback could have seen.
    """
    log_path = Path(log_path)
    if not log_path.is_absolute():
        from agent.config import repo_root
        log_path = repo_root() / log_path

    if not text_only:
        from_sidecar = read_sidecar(log_path)
        if from_sidecar is not None:
            return from_sidecar

    if not log_path.exists():
        return []

    failures: list[dict[str, Any]] = []
    seen_vars: set[str] = set()

    for line in log_path.read_text(errors="replace").splitlines():
        stripped = line.strip()

        m = MODULE_RE.search(stripped)
        if m:
            failures.append({
                "type": "removed_module",
                "module": m.group(1),
                "module_short": short_module_name(m.group(1)),
                "message": stripped,
            })
            continue

        m = NO_HOSTS_RE.search(stripped)
        if m:
            failures.append({
                "type": "no_hosts_matched",
                "host": m.group(1),
                "pattern": m.group(1),
                "message": stripped,
            })
            continue

        m = UNREACHABLE_MOCK_RE.search(stripped)
        if m:
            failures.append({
                "type": "unreachable_host",
                "host": m.group(1),
                "pattern": m.group(2),
                "message": stripped,
            })
            continue
        m = UNREACHABLE_RE.search(stripped)
        if m:
            failures.append({
                "type": "unreachable_host",
                "host": m.group(1),
                "message": stripped,
            })
            continue

        m = UNDEFVAR_MOCK_RE.search(stripped) or UNDEFVAR_REAL_RE.search(stripped)
        if m:
            var = m.group(1)
            # Real Ansible prints the same undefined-variable error several
            # times (origin block, then the fatal line). One failure per name.
            if var in seen_vars:
                continue
            seen_vars.add(var)
            failures.append({
                "type": "undefined_variable",
                "variable": var,
                "message": stripped,
            })
            continue

    return failures


def summarise_failures(failures: list[dict]) -> str:
    """Return a one-line human summary of the failure list."""
    if not failures:
        return "no failures detected"
    types = sorted({f["type"] for f in failures})
    return f"{len(failures)} failure(s) across {len(types)} type(s): {', '.join(types)}"
