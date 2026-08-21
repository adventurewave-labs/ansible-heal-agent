"""Diagnoser — turn a raw failure into an actionable fix plan.

Primary path: ask the LLM (via agent.llm) for a structured diagnosis.
Fallback path: a deterministic rule-based diagnoser that handles the three
shipped failure classes. The fallback exists so the demo never gets stuck if
the LLM is rate-limited, offline, or returns garbage.
"""

from __future__ import annotations

import json
from typing import Any

from agent import llm
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
  "failure_type": "unreachable_host | removed_module | undefined_variable | other",
  "fix": {{
    "action": "edit_file",
    "target_file": "<relative path under repo root>",
    "search": "<exact substring to find in the file>",
    "replace": "<exact substring to replace it with>",
    "rationale": "<one sentence>"
  }}
}}

Rules:
- target_file MUST be one of: ansible/inventory.yml, ansible/group_vars/all.yml,
  ansible/playbooks/webservers.yml, ansible/playbooks/db.yml, ansible/playbooks/site.yml.
- search MUST be an exact substring that appears verbatim in the target file.
- replace MUST be syntactically valid YAML fragment that fits in place of `search`.
- For unreachable_host: rename the host in inventory.yml to match the playbook's
  expectation, OR fix the playbook's `hosts:` line. Prefer editing inventory.
- For removed_module: replace the deprecated module task with a modern equivalent.
  For apt_key → use ansible.builtin.get_url to fetch the key, then
  ansible.builtin.command: apt-key add. Or simply drop the key task if the
  package is already trusted.
- For undefined_variable: add the missing variable to ansible/group_vars/all.yml
  with a sensible default value.
"""


def _load_context(failure: dict, repo_root) -> str:
    """Load the most relevant files for the failure type."""
    paths = [
        "ansible/inventory.yml",
        "ansible/group_vars/all.yml",
        "ansible/playbooks/webservers.yml",
        "ansible/playbooks/db.yml",
    ]
    chunks = []
    for rel in paths:
        full = repo_root / rel
        if full.exists():
            chunks.append(f"--- {rel} ---\n{full.read_text()}")
    return "\n\n".join(chunks)


def llm_diagnose(failure: dict) -> dict[str, Any]:
    """Call the LLM and return its structured diagnosis."""
    prompt = PROMPT_TEMPLATE.format(
        failure_json=json.dumps(failure, indent=2),
        context=_load_context(failure, repo_root()),
    )
    return llm.chat_json(prompt, system=SYSTEM_PROMPT)


def fallback_diagnose(failure: dict) -> dict[str, Any]:
    """Deterministic rule-based diagnoser for the three shipped failure classes."""
    ftype = failure.get("type")

    if ftype == "unreachable_host":
        # We expect the playbook to target 'web-server-01' but inventory has 'web-01'.
        # Heuristic: rename inventory entry to match the playbook's host pattern.
        # The 'host' field in the failure is the pattern that failed (e.g. web-server-01).
        target = failure.get("host") or "web-server-01"
        return {
            "diagnosis": f"Inventory references stale hostname; playbook expects '{target}'.",
            "failure_type": "unreachable_host",
            "fix": {
                "action": "edit_file",
                "target_file": "ansible/inventory.yml",
                "search": "web-01:",
                "replace": f"{target}:",
                "rationale": "Rename the inventory entry to match the playbook's `hosts:` pattern.",
            },
        }

    if ftype == "removed_module":
        return {
            "diagnosis": "Playbook uses the removed `apt_key` module.",
            "failure_type": "removed_module",
            "fix": {
                "action": "edit_file",
                "target_file": "ansible/playbooks/webservers.yml",
                "search": (
                    "    - name: Add nginx signing key (DEPRECATED MODULE)\n"
                    "      ansible.builtin.apt_key:\n"
                    "        url: https://nginx.org/keys/nginx_signing.key\n"
                    "        state: present\n"
                ),
                "replace": (
                    "    - name: Add nginx signing key (modern)\n"
                    "      ansible.builtin.get_url:\n"
                    "        url: https://nginx.org/keys/nginx_signing.key\n"
                    "        dest: /usr/share/keyrings/nginx-archive-keyring.gpg\n"
                    "        mode: '0644'\n"
                ),
                "rationale": ("apt_key was removed in ansible-core 2.18; "
                              "get_url to the keyring is the modern equivalent."),
            },
        }

    if ftype == "undefined_variable":
        var = failure.get("variable", "nginx_port")
        return {
            "diagnosis": f"Playbook references undefined variable '{var}'.",
            "failure_type": "undefined_variable",
            "fix": {
                "action": "edit_file",
                "target_file": "ansible/group_vars/all.yml",
                "search": "# (nginx_port is intentionally omitted — see scenarios/seed.py)",
                "replace": "# Added by ansible-heal-agent\nnginx_port: 8080",
                "rationale": (f"Add a sensible default for '{var}' to group_vars "
                              "so the template can render."),
            },
        }

    return {
        "diagnosis": "Unknown failure type — no automated fix available.",
        "failure_type": "other",
        "fix": {"action": "none"},
    }


def diagnose(failure: dict, use_llm: bool = True) -> dict[str, Any]:
    """Top-level entry: try LLM first, fall back to rule-based on any error."""
    if not use_llm:
        return fallback_diagnose(failure)
    try:
        result = llm_diagnose(failure)
        # Validate shape
        if "fix" not in result or "target_file" not in result.get("fix", {}):
            raise ValueError("LLM diagnosis missing required keys")
        return result
    except Exception as e:  # noqa: BLE001
        # Mark fallback origin so the transcript is honest
        fb = fallback_diagnose(failure)
        fb["_fallback_reason"] = str(e)
        return fb
