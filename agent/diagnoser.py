"""Diagnoser — turn a raw failure into an actionable fix plan.

Primary path: ask the LLM (via ``agent.llm``) for a structured diagnosis.

Fallback path: a deterministic diagnoser that **derives** the fix from the
failure payload and the current state of the repository. The previous version
returned hardcoded search/replace pairs matched to one seeded scenario — it
searched for the literal string ``web-01:``, and for an undefined variable it
inserted ``nginx_port: 8080`` regardless of which variable was actually
undefined. Renaming the variable made it loop three times and give up.

Nothing here is anchored on a literal from the demo baseline. Each rule reads
the file it intends to change and works out the edit from what is there.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from agent import llm, yaml_edit
from agent.config import repo_root

SYSTEM_PROMPT = (
    "You are an SRE agent specialised in Ansible. Given a single pipeline failure "
    "and the relevant playbook/inventory context, you output a JSON object describing "
    "the smallest, safest fix. You do not editorialise. You output JSON only."
)

PROMPT_TEMPLATE = """You are diagnosing an Ansible pipeline failure.

FAILURE:
{failure_json}

CONTEXT (relevant files in the repo):
{context}

Return a JSON object with EXACTLY this shape:
{{
  "diagnosis": "<one-sentence root cause>",
  "failure_type": "unreachable_host | no_hosts_matched | removed_module
                   | undefined_variable | other",
  "fix": {{
    "action": "edit_file",
    "target_file": "<relative path under the repo root>",
    "search": "<exact substring to find in the file>",
    "replace": "<exact substring to replace it with>",
    "rationale": "<one sentence>"
  }}
}}

Rules:
- target_file MUST be inside the agent's write surface (by default ansible/**).
- search MUST be an exact substring that appears verbatim in the target file.
- replace MUST be a syntactically valid YAML fragment that fits in place of search.
- For an unmatched or unreachable host: rename the host in the inventory to the
  name the playbook targets. Prefer editing the inventory over the playbook.
- For a removed module: replace the task's module with its modern equivalent.
- For an undefined variable: add it to the group_vars file with a sensible
  default.
"""

#: Modules removed or renamed upstream, and what to use instead. Keyed on the
#: short name so both ``apt_key`` and ``ansible.builtin.apt_key`` resolve.
MODULE_REPLACEMENTS: dict[str, dict[str, Any]] = {
    "apt_key": {
        "module": "ansible.builtin.get_url",
        # Checked against ansible-core 2.19: apt_key still *resolves* — it is
        # deprecated, not yet removed — so this replacement is a modernisation,
        # not a fix for an unresolvable module. The mock runner reports it as a
        # removed module because the seeded scenario predates that check.
        "note": "apt_key is deprecated and slated for removal; fetch the key "
                "into /usr/share/keyrings and reference it explicitly instead.",
        "args": lambda old: {
            "url": old.get("url", ""),
            "dest": "/usr/share/keyrings/"
                    f"{_keyring_name(old.get('url', ''))}.gpg",
            "mode": "0644",
        },
    },
    "docker": {
        "module": "community.docker.docker_container",
        "note": "the bundled docker module was removed; use the "
                "community.docker collection.",
        "args": lambda old: dict(old),
    },
    "docker_container": {
        "module": "community.docker.docker_container",
        "note": "moved to the community.docker collection.",
        "args": lambda old: dict(old),
    },
}

#: Files searched, in order, for the playbook that uses a removed module.
_PLAYBOOK_GLOB = "ansible/playbooks/*.yml"

#: Files searched, in order, for somewhere to define a missing variable.
_GROUP_VARS_CANDIDATES = (
    "ansible/group_vars/all.yml",
    "ansible/group_vars/all.yaml",
    "ansible/group_vars/all/main.yml",
)

_INVENTORY = "ansible/inventory.yml"


def _keyring_name(url: str) -> str:
    stem = Path(url).stem or "archive"
    return f"{stem}-keyring".replace("_", "-")


def _read(rel: str) -> str | None:
    path = repo_root() / rel
    return path.read_text() if path.is_file() else None


def _no_fix(reason: str, ftype: str = "other") -> dict[str, Any]:
    """A diagnosis that honestly reports it has no automated fix."""
    return {
        "diagnosis": reason,
        "failure_type": ftype,
        "fix": {"action": "none"},
        "_no_fix_reason": reason,
    }


# ── LLM path ───────────────────────────────────────────────────────

def _load_context(failure: dict, root: Path) -> str:
    """Load the files most relevant to this failure."""
    paths = [_INVENTORY, *_GROUP_VARS_CANDIDATES]
    paths += sorted(str(p.relative_to(root))
                    for p in root.glob(_PLAYBOOK_GLOB))
    chunks = []
    for rel in paths:
        full = root / rel
        if full.is_file():
            chunks.append(f"--- {rel} ---\n{full.read_text()}")
    return "\n\n".join(chunks)


def llm_diagnose(failure: dict) -> dict[str, Any]:
    """Call the LLM and return its structured diagnosis."""
    prompt = PROMPT_TEMPLATE.format(
        failure_json=json.dumps(failure, indent=2),
        context=_load_context(failure, repo_root()),
    )
    return llm.chat_json(prompt, system=SYSTEM_PROMPT)


# ── deterministic rules ─────────────────────────────────────────────

def _diagnose_host(failure: dict) -> dict[str, Any]:
    """A play targets a host the inventory does not contain.

    The fix renames the inventory entry that was *meant* to be that host. Which
    entry that is comes from the inventory itself: the closest existing name to
    the one the playbook expects. If nothing is close enough, the agent says so
    rather than renaming an unrelated host.
    """
    expected = failure.get("pattern") or failure.get("host")
    if not expected:
        return _no_fix("failure did not name the host pattern that failed")

    inventory = _read(_INVENTORY)
    if inventory is None:
        return _no_fix(f"no inventory at {_INVENTORY} to reconcile against")

    hosts = yaml_edit.inventory_hosts(inventory)
    if expected in hosts:
        return _no_fix(
            f"'{expected}' is already in the inventory; the failure is not a "
            f"stale hostname")

    matches = difflib.get_close_matches(expected, hosts, n=1, cutoff=0.6)
    if not matches:
        return _no_fix(
            f"no inventory host resembles '{expected}' (inventory has "
            f"{hosts or 'no hosts'}); renaming an unrelated host would be a "
            f"guess, so no fix is proposed")

    stale = matches[0]
    return {
        "diagnosis": f"Inventory lists '{stale}' but the playbook targets "
                     f"'{expected}'.",
        "failure_type": failure.get("type", "unreachable_host"),
        "fix": {
            "action": "rename_host",
            "target_file": _INVENTORY,
            "old": stale,
            "new": expected,
            "rationale": f"Rename '{stale}' to '{expected}' so the play's "
                         f"hosts: pattern resolves.",
        },
    }


def _diagnose_variable(failure: dict) -> dict[str, Any]:
    """A playbook references a variable nothing defines."""
    var = failure.get("variable")
    if not var:
        return _no_fix("failure did not name the undefined variable")

    for rel in _GROUP_VARS_CANDIDATES:
        if _read(rel) is not None:
            target = rel
            break
    else:
        return _no_fix(
            f"no group_vars file found (looked in {list(_GROUP_VARS_CANDIDATES)}) "
            f"to define '{var}' in")

    value = yaml_edit.infer_default(var)
    shown = "an empty placeholder" if value == "" else repr(value)
    return {
        "diagnosis": f"Playbook references undefined variable '{var}'.",
        "failure_type": "undefined_variable",
        "fix": {
            "action": "set_yaml_key",
            "target_file": target,
            "key": var,
            "value": value,
            "rationale": f"Define '{var}' in {target} with {shown} so the "
                         f"template can render.",
        },
    }


def _find_playbook_using(module: str) -> str | None:
    """Return the repo-relative playbook that uses ``module``, if any."""
    root = repo_root()
    for path in sorted(root.glob(_PLAYBOOK_GLOB)):
        if yaml_edit.find_module_use(path.read_text(), module):
            return str(path.relative_to(root))
    return None


def _diagnose_module(failure: dict) -> dict[str, Any]:
    """A task uses a module that this ansible-core no longer resolves."""
    module = failure.get("module") or failure.get("module_short")
    if not module:
        return _no_fix("failure did not name the unresolvable module")

    short = module.rsplit(".", 1)[-1]
    replacement = MODULE_REPLACEMENTS.get(short)
    if replacement is None:
        return _no_fix(
            f"no known replacement for module '{module}'; add one to "
            f"MODULE_REPLACEMENTS to teach the agent this migration")

    playbook = _find_playbook_using(short)
    if playbook is None:
        return _no_fix(f"no playbook under {_PLAYBOOK_GLOB} uses '{module}'")

    old_args = _existing_args(playbook, short)
    return {
        "diagnosis": f"Playbook uses the removed `{short}` module.",
        "failure_type": "removed_module",
        "fix": {
            "action": "replace_module",
            "target_file": playbook,
            "old_module": short,
            "new_module": replacement["module"],
            "new_args": replacement["args"](old_args),
            "rationale": replacement["note"],
        },
    }


def _existing_args(playbook_rel: str, short_module: str) -> dict[str, Any]:
    """Return the arguments the task currently passes to ``short_module``."""
    text = _read(playbook_rel) or ""
    try:
        data = yaml_edit.load(text)
    except yaml_edit.YamlEditError:
        return {}
    if not isinstance(data, list):
        return {}
    for play in data:
        if not isinstance(play, dict):
            continue
        for section in ("tasks", "pre_tasks", "post_tasks", "handlers"):
            for task in play.get(section) or []:
                if not isinstance(task, dict):
                    continue
                for key, value in task.items():
                    if key.rsplit(".", 1)[-1] == short_module:
                        return dict(value) if isinstance(value, dict) else {}
    return {}


_RULES = {
    "unreachable_host": _diagnose_host,
    "no_hosts_matched": _diagnose_host,
    "undefined_variable": _diagnose_variable,
    "removed_module": _diagnose_module,
}


def fallback_diagnose(failure: dict) -> dict[str, Any]:
    """Deterministic diagnoser. Derives the fix from the repo, not a lookup."""
    rule = _RULES.get(failure.get("type"))
    if rule is None:
        return _no_fix(
            f"no rule for failure type {failure.get('type')!r}; "
            f"no automated fix available")
    return rule(failure)


def diagnose(failure: dict, use_llm: bool = True) -> dict[str, Any]:
    """Try the LLM first, fall back to the deterministic rules on any error."""
    if not use_llm:
        return fallback_diagnose(failure)
    try:
        result = llm_diagnose(failure)
        if "fix" not in result or "target_file" not in result.get("fix", {}):
            raise ValueError("LLM diagnosis missing required keys")
        return result
    except Exception as e:  # noqa: BLE001
        fb = fallback_diagnose(failure)
        fb["_fallback_reason"] = str(e)
        return fb
