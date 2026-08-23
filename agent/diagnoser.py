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
import ipaddress
import json
from pathlib import Path
from typing import Any

import yaml

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

_DEFAULT_INVENTORY = "ansible/inventory.yml"


def _inventory_rel() -> str:
    """The inventory the run used, repo-relative — ansible.cfg may move it."""
    try:
        from pipeline.runner import inventory_path
        return str(inventory_path().relative_to(repo_root().resolve()))
    except Exception:
        return _DEFAULT_INVENTORY


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
    paths = [_inventory_rel(), *_GROUP_VARS_CANDIDATES]
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

#: Characters that make a `hosts:` value a *pattern* rather than a hostname:
#: separators, exclusion, intersection, ranges, regex and globs.
_PATTERN_METACHARACTERS = set("!&[]*?~,;")


def _looks_like_ipv6(text: str) -> bool:
    try:
        ipaddress.IPv6Address(text.strip("[]"))
    except ValueError:
        return False
    return True


def _not_a_single_hostname(expected: str) -> str | None:
    """Why ``expected`` cannot be treated as one stale hostname, or None."""
    text = str(expected).strip()
    if not text:
        return "the failing play has an empty hosts: pattern; Ansible rejects that outright"
    if text.lower() == "localhost":
        return ("'localhost' is always available to Ansible implicitly; a play "
                "targeting it does not need an inventory entry, and renaming a "
                "real host to 'localhost' would redirect every later "
                "localhost play at that machine")
    if text in ("all", "*"):
        return (f"'{text}' matches every host; that it matched none means the "
                f"inventory is empty, which renaming cannot fix")
    if any(c.isspace() for c in text):
        return (f"{text!r} is a multi-host pattern (Ansible splits on "
                f"whitespace); it is not a hostname to rename to")
    # ":" unions patterns — except inside an IPv6 literal, which is a hostname.
    if ":" in text and not _looks_like_ipv6(text):
        return (f"{text!r} is an Ansible pattern, not a hostname (\':\' unions "
                f"patterns); renaming an inventory entry to it would create a "
                f"host that no pattern resolves to")
    bad = sorted(_PATTERN_METACHARACTERS & set(text))
    if bad:
        return (f"{text!r} is an Ansible pattern, not a hostname "
                f"(contains {''.join(bad)!r}); renaming an inventory entry to "
                f"it would create a host that no pattern resolves to")
    return None


def _play_host_patterns() -> dict[str, str]:
    """Every ``hosts:`` pattern in the repo's playbooks, mapped to its file.

    Renaming an inventory entry is only safe if no *other* play depends on the
    name being renamed away. Without this, two plays targeting different hosts
    against a one-host inventory make the agent rename that host back and forth
    once per iteration: it "progresses" every round, so the stall detector never
    fires, and it lands a junk commit each time.
    """
    patterns: dict[str, str] = {}
    for path in sorted(repo_root().glob(_PLAYBOOK_GLOB)):
        try:
            plays = yaml.safe_load(path.read_text()) or []
        except yaml.YAMLError:
            continue
        if not isinstance(plays, list):
            continue
        for play in plays:
            if not isinstance(play, dict):
                continue
            hosts = play.get("hosts")
            if isinstance(hosts, str):
                patterns.setdefault(hosts, path.name)
            elif isinstance(hosts, list):
                for h in hosts:
                    patterns.setdefault(str(h), path.name)
    return patterns


def _diagnose_host(failure: dict) -> dict[str, Any]:
    """A play targets a host the inventory does not contain.

    The fix renames the inventory entry that was *meant* to be that host. Which
    entry that is comes from the inventory itself: the closest existing name to
    the one the playbook expects. If nothing is close enough — or if renaming it
    would break a different play — the agent says so rather than guessing.
    """
    expected = failure.get("pattern") or failure.get("host")
    if not expected:
        return _no_fix("failure did not name the host pattern that failed")

    # Renaming an inventory entry is only defensible when the thing that failed
    # to match is unambiguously one stale hostname. Everything below is a case
    # where it is not, and where renaming destroyed a working repository:
    # a multi-host pattern became a host literally called "web-01 db-01"; a
    # group name became a host colliding with the group; localhost, which
    # Ansible always provides implicitly, was repointed at a remote address.
    # Check the pattern as written, not the fragment a runner split out of it:
    # `fd00::21` reached this guard as the token `21`, looked like a perfectly
    # ordinary hostname, and a real IPv6 address was renamed to `fd00`.
    as_written = failure.get("raw_pattern") or expected
    unambiguous = (_not_a_single_hostname(as_written)
                   or _not_a_single_hostname(expected))
    if unambiguous:
        return _no_fix(unambiguous)

    inventory = _read(_inventory_rel())
    if inventory is None:
        return _no_fix(f"no inventory at {_inventory_rel()} to reconcile against")

    try:
        hosts = yaml_edit.inventory_hosts(inventory)
    except Exception as e:
        # An INI inventory, or YAML this editor cannot round-trip. Ansible may
        # well read it fine; the agent simply cannot edit it safely.
        return _no_fix(
            f"cannot parse {_inventory_rel()} well enough to edit it safely: {e}")

    groups = yaml_edit.inventory_groups(inventory) if hasattr(
        yaml_edit, "inventory_groups") else set()
    if expected in groups:
        return _no_fix(
            f"'{expected}' is a group in {_inventory_rel()}, not a host. Renaming a "
            f"host to match it would create a group/host name collision; the "
            f"group is more likely empty or dynamically populated")
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
    if stale in groups:
        return _no_fix(
            f"the closest inventory name to '{expected}' is the group "
            f"'{stale}'; renaming a group is not a host fix")

    claimed_by = _play_host_patterns().get(stale)
    if claimed_by:
        return _no_fix(
            f"'{stale}' is the closest inventory host to '{expected}', but "
            f"{claimed_by} still targets '{stale}'; renaming it would break "
            f"that play, so no fix is proposed")

    return {
        "diagnosis": f"Inventory lists '{stale}' but the playbook targets "
                     f"'{expected}'.",
        "failure_type": failure.get("type", "unreachable_host"),
        "fix": {
            "action": "rename_host",
            "target_file": _inventory_rel(),
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
        # Carry the failure's own message through. Some types exist precisely to
        # say something useful — an unreadable file, a host pattern the
        # simulator cannot evaluate — and swallowing that left the operator with
        # only a type name to go on.
        detail = (failure.get("message") or "").strip()
        return _no_fix(
            f"no automated fix for {failure.get('type')!r}"
            + (f": {detail}" if detail else "; no automated fix available"))
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
