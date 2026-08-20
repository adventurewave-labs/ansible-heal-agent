"""Log scanner — extract structured failures from a mock ansible-playbook log.

Real Ansible logs are noisy. The scanner walks the log line by line, captures
both the human-readable failure line and the structured failure dictionaries
emitted by pipeline/runner.py (which are written to a sibling JSON file).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Patterns matched against raw log lines.
UNREACHABLE_RE = re.compile(r"fatal:\s*\[([^\]]+)\]:\s*UNREACHABLE!.*Host '([^']+)' not found")
MODULE_RE = re.compile(r"ERROR!\s*couldn't resolve module action '([^']+)'")
UNDEFVAR_RE = re.compile(r"undefined variable '([^']+)'")


def extract_failures(log_path: str | Path) -> list[dict[str, Any]]:
    """Return a list of failure dicts, ordered by appearance in the log.

    Each dict has at minimum: ``type``, ``host`` (optional), ``message``.
    """
    log_path = Path(log_path)
    if not log_path.is_absolute():
        # Resolve relative to repo root.
        from pipeline.git_helper import REPO_ROOT
        log_path = REPO_ROOT / log_path
    text = log_path.read_text()
    failures: list[dict[str, Any]] = []

    # 1) Structured sidecar (preferred — written by runner.py for tests).
    sidecar = log_path.with_suffix(".json")
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    # 2) Fallback: regex over log lines.
    for line in text.splitlines():
        m = UNREACHABLE_RE.search(line)
        if m:
            failures.append({
                "type": "unreachable_host",
                "host": m.group(1),
                "pattern": m.group(2),
                "message": line.strip(),
            })
            continue
        m = MODULE_RE.search(line)
        if m:
            failures.append({
                "type": "removed_module",
                "module": m.group(1),
                "message": line.strip(),
            })
            continue
        m = UNDEFVAR_RE.search(line)
        if m:
            failures.append({
                "type": "undefined_variable",
                "variable": m.group(1),
                "message": line.strip(),
            })
            continue

    return failures


def summarise_failures(failures: list[dict]) -> str:
    """Return a one-line human summary of the failure list."""
    if not failures:
        return "no failures detected"
    types = sorted({f["type"] for f in failures})
    return f"{len(failures)} failure(s) across {len(types)} type(s): {', '.join(types)}"
