"""SARIF 2.1.0 output — dry-run findings as GitHub code-scanning alerts.

``ansible-heal run --dry-run --sarif out.sarif`` writes every failure the
pipeline produced as a SARIF result, so an Ansible repository gets
pull-request annotations and a Security → Code scanning view without the agent
ever writing to it. Each result carries:

- a **rule** per failure class (``AHA001`` … ``AHA004``, plus ``AHA900`` for
  failures the agent has no rule for);
- a **location** at the line in the playbook that triggered it — the
  ``hosts:`` pattern, the variable reference, the module key — found by text
  search, falling back to line 1 rather than inventing a region;
- a **level**: ``warning`` when the agent has a validated fix, ``note`` when it
  declines (a refusal is still information), ``error`` when the proposed
  write was blocked by the write surface;
- the proposed **diff** and the agent's diagnosis in the message and in
  ``properties``, so a reviewer sees the exact change the apply mode would make;
- a stable ``partialFingerprints`` entry, so re-running on an unchanged
  repository updates alerts rather than duplicating them.

Pure functions over :class:`agent.core.HealResult`; no I/O except
:func:`write`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent.config import repo_root

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
TOOL_NAME = "ansible-heal-agent"
INFO_URI = "https://github.com/adventurewave-labs/ansible-heal-agent"

RULES: dict[str, dict[str, str]] = {
    "AHA001": {"name": "host-pattern-matches-nothing",
               "short": "A play's hosts: pattern matches no inventory host",
               "types": "no_hosts_matched,unreachable_host"},
    "AHA002": {"name": "undefined-variable",
               "short": "A variable is referenced but defined nowhere Ansible looks",
               "types": "undefined_variable,narrowly_defined_variable"},
    "AHA003": {"name": "unresolvable-module",
               "short": "A task uses a module ansible-core cannot resolve",
               "types": "removed_module"},
    "AHA004": {"name": "missing-collection",
               "short": "A module's collection is neither installed nor declared",
               "types": "missing_collection"},
    "AHA900": {"name": "unclassified-failure",
               "short": "A pipeline failure the agent has no rule for",
               "types": ""},
}

_TYPE_TO_RULE = {t: rid for rid, r in RULES.items() for t in r["types"].split(",") if t}


def rule_for(failure: dict, diagnosis: dict | None = None) -> str:
    ftype = (diagnosis or {}).get("failure_type")
    if ftype in _TYPE_TO_RULE:
        return _TYPE_TO_RULE[ftype]
    return _TYPE_TO_RULE.get(failure.get("type", ""), "AHA900")


def _needle(failure: dict) -> str | None:
    """The token in the playbook that the failure is about."""
    for key in ("variable", "module", "pattern", "host"):
        value = failure.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _locate(rel: str | None, needle: str | None) -> dict[str, Any] | None:
    """A SARIF physicalLocation for ``needle`` inside ``rel``."""
    if not rel:
        return None
    line = 1
    try:
        text = (repo_root() / rel).read_text(errors="replace")
    except OSError:
        text = ""
    if needle and text:
        short = needle.rsplit(".", 1)[-1]
        for i, row in enumerate(text.splitlines(), start=1):
            if needle in row or (short and f"{short}:" in row):
                line = i
                break
    return {"physicalLocation": {
        "artifactLocation": {"uri": rel, "uriBaseId": "%SRCROOT%"},
        "region": {"startLine": line},
    }}


def _fingerprint(rule: str, failure: dict) -> str:
    key = "|".join([rule, str(failure.get("playbook", "")),
                    str(_needle(failure) or ""), str(failure.get("task", ""))])
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _result(proposal: Any) -> dict[str, Any]:
    failure, diag = proposal.failure, proposal.diagnosis or {}
    rule = rule_for(failure, diag)
    fix = diag.get("fix") or {}
    has_fix = bool(proposal.diff) and not proposal.blocked_reason
    if proposal.blocked_reason:
        level = "error"
    elif has_fix:
        level = "warning"
    else:
        level = "note"

    headline = diag.get("diagnosis") or failure.get("message") or failure.get("type", "failure")
    if proposal.blocked_reason:
        tail = f"Proposed fix blocked: {proposal.blocked_reason}"
    elif has_fix:
        tail = f"Proposed fix ({fix.get('action')} on {proposal.target_file}): " \
               f"{fix.get('rationale', '')}".rstrip(": ")
    else:
        tail = "No automated fix: " + (diag.get("_no_fix_reason") or "none available")
    text = f"{headline} {tail}"
    markdown = f"**{headline}**\n\n{tail}"
    if has_fix:
        markdown += f"\n\n```diff\n{proposal.diff.rstrip()}\n```"

    source = failure.get("playbook") or proposal.target_file
    locations = [loc for loc in [_locate(source, _needle(failure))] if loc]
    related = []
    if proposal.target_file and proposal.target_file != source:
        loc = _locate(proposal.target_file, None)
        if loc:
            loc["id"] = 1
            loc["message"] = {"text": "file the fix would change"}
            related.append(loc)

    out: dict[str, Any] = {
        "ruleId": rule,
        "ruleIndex": list(RULES).index(rule),
        "level": level,
        "message": {"text": text, "markdown": markdown},
        "locations": locations,
        "partialFingerprints": {"ansibleHealFailure/v1": _fingerprint(rule, failure)},
        "properties": {
            "failureType": failure.get("type"),
            "fixAction": fix.get("action"),
            "targetFile": proposal.target_file,
            "diff": proposal.diff or None,
            "llmFallbackReason": diag.get("_fallback_reason"),
        },
    }
    if related:
        out["relatedLocations"] = related
    return out


def to_sarif(result: Any, version: str = "0.1.0") -> dict[str, Any]:
    """Build a SARIF 2.1.0 log from a (dry-run) :class:`HealResult`."""
    rules = [{
        "id": rid,
        "name": r["name"],
        "shortDescription": {"text": r["short"]},
        "helpUri": f"{INFO_URI}#where-it-declines",
        "defaultConfiguration": {"level": "warning"},
    } for rid, r in RULES.items()]
    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": TOOL_NAME, "version": version,
                "informationUri": INFO_URI, "rules": rules,
            }},
            "originalUriBaseIds": {"%SRCROOT%": {"uri": repo_root().as_uri() + "/"}},
            "results": [_result(p) for p in result.proposals],
            "properties": {
                "mode": result.mode,
                "pipelineExitCode": result.final_exit_code,
                "declined": list(result.declined),
                "blocked": list(result.blocked),
            },
        }],
    }


def write(result: Any, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(to_sarif(result), indent=2) + "\n")
    return out
