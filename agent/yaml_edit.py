"""Structural YAML edits that preserve formatting and comments.

The fallback diagnoser used to express every fix as a literal search/replace
pair — including one anchored on a *comment string* in group_vars. That is why
it could only ever heal the exact three seeded failures: change the variable
name and the anchor no longer exists.

These helpers edit the document instead of the text, via ruamel.yaml's
round-trip mode, so indentation, key order and comments survive. Each returns
the new file content as a string; the patcher decides whether to write it.
"""

from __future__ import annotations

import io
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


class YamlEditError(RuntimeError):
    pass


def _yaml(explicit_start: bool = False) -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096          # never rewrap a line we did not touch
    y.indent(mapping=2, sequence=4, offset=2)
    y.explicit_start = explicit_start
    return y


def has_document_start(text: str) -> bool:
    """True if the document opens with an explicit ``---`` marker."""
    for line in text.splitlines():
        if line.strip():
            return line.strip() == "---"
    return False


def load(text: str) -> Any:
    try:
        return _yaml().load(text)
    except YAMLError as e:
        raise YamlEditError(f"could not parse YAML: {e}") from e


def dump(data: Any, explicit_start: bool = False) -> str:
    """Serialise ``data``. ``explicit_start`` re-emits the ``---`` marker.

    Two bits of tidying, both there to keep the agent's diffs reviewable:

    * the ``---`` marker is restored if the file had one, instead of silently
      disappearing from every patched file;
    * a root-level block sequence (i.e. a playbook) is dedented back to column
      zero. ruamel applies its sequence offset at every level including the
      root, which would otherwise re-indent every line of every playbook the
      agent touches and bury the real change in the diff.
    """
    buf = io.StringIO()
    _yaml(explicit_start).dump(data, buf)
    out = buf.getvalue()
    if isinstance(data, list):
        out = _dedent_root_sequence(out)
    return out


def _dedent_root_sequence(text: str, width: int = 2) -> str:
    """Pull an indented root-level block sequence back to column zero.

    Full-line comments emitted at column zero are left where they are; only
    lines carrying the sequence's own indent are shifted.
    """
    lines = text.splitlines(keepends=True)
    prefix = " " * width
    item = prefix + "- "

    def is_structural(line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and stripped != "---" and not stripped.startswith("#")

    structural = [ln for ln in lines if is_structural(ln)]
    if not structural:
        return text
    # Already flat: nothing to do.
    if any(ln.startswith("- ") for ln in structural):
        return text
    # Only dedent when the root really is an indented sequence.
    if not any(ln.startswith(item) for ln in structural):
        return text
    if not all(ln.startswith(prefix) for ln in structural):
        return text

    return "".join(ln[width:] if ln.startswith(prefix) else ln for ln in lines)


# ── variable defaults ───────────────────────────────────────────────

#: Suffix -> default. Deliberately conservative: the agent's job is to make the
#: template render, not to guess your production port. Anything not matched here
#: gets an empty string and a comment saying so, which is honest and still
#: unblocks the pipeline.
_DEFAULTS: tuple[tuple[str, Any], ...] = (
    ("_port", 8080),
    ("_timeout", 30),
    ("_retries", 3),
    ("_replicas", 1),
    ("_count", 1),
    ("_enabled", True),
    ("_enable", True),
    ("_debug", False),
    ("_path", ""),
    ("_dir", ""),
    ("_user", ""),
    ("_host", ""),
)


def infer_default(var: str) -> Any:
    """Guess a safe placeholder value for an undefined variable name."""
    lowered = var.lower()
    for suffix, value in _DEFAULTS:
        if lowered.endswith(suffix):
            return value
    if lowered.startswith("enable_") or lowered.startswith("use_"):
        return False
    return ""


def set_key(text: str, key: str, value: Any) -> str:
    """Return ``text`` with top-level ``key`` set to ``value``.

    Raises YamlEditError if the key already exists — the caller wanted to add a
    missing variable, and silently overwriting an existing one would be a very
    different, much less safe operation.
    """
    data = load(text)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise YamlEditError("expected a mapping at the top level")
    if key in data:
        raise YamlEditError(f"{key!r} is already defined; refusing to overwrite")
    data[key] = value
    return dump(data, explicit_start=has_document_start(text))


# ── inventory ───────────────────────────────────────────────────────

def inventory_hosts(text: str) -> list[str]:
    """Return every concrete host name in an inventory document."""
    data = load(text)
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "hosts" and isinstance(v, dict):
                    found.extend(v.keys())
                elif isinstance(v, dict):
                    walk(v)

    walk(data if isinstance(data, dict) else {})
    return found


def inventory_groups(text: str) -> set[str]:
    """Every group name in an inventory document.

    Needed so the diagnoser never renames a host to match a *group* pattern —
    an empty group (a scaled-to-zero tier, a dynamic-inventory placeholder)
    makes Ansible skip the play and exit 0, and "fixing" it by renaming the
    nearest host both deletes that host and creates a group/host collision.
    """
    data = load(text)
    groups: set[str] = set()

    def walk(node, inside_group_container: bool):
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            if k in ("hosts", "vars"):
                continue
            if k == "children" and isinstance(v, dict):
                for name, child in v.items():
                    groups.add(str(name))
                    walk(child, False)
            elif isinstance(v, dict):
                # A mapping that carries hosts/children/vars is a group.
                if any(key in v for key in ("hosts", "children", "vars")):
                    groups.add(str(k))
                walk(v, False)

    if isinstance(data, dict):
        for top, node in data.items():
            groups.add(str(top))
            walk(node, True)
    return groups


def rename_host(text: str, old: str, new: str) -> str:
    """Rename a host key in an inventory, preserving its position and vars."""
    data = load(text)
    renamed = False

    def walk(node):
        nonlocal renamed
        if not isinstance(node, dict):
            return
        for k, v in list(node.items()):
            if k == "hosts" and isinstance(v, dict) and old in v:
                # Rebuild in place so ordering and comments are preserved.
                items = list(v.items())
                v.clear()
                for hk, hv in items:
                    v[new if hk == old else hk] = hv
                renamed = True
            elif isinstance(v, dict):
                walk(v)

    walk(data if isinstance(data, dict) else {})
    if not renamed:
        raise YamlEditError(f"host {old!r} not found in inventory")
    return dump(data, explicit_start=has_document_start(text))


# ── playbook tasks ──────────────────────────────────────────────────

def _module_keys(task: dict) -> list[str]:
    """Keys of a task dict that name a module (not a directive)."""
    directives = {
        "name", "when", "become", "become_user", "register", "vars", "tags",
        "notify", "loop", "with_items", "ignore_errors", "changed_when",
        "failed_when", "delegate_to", "run_once", "no_log", "environment",
        "args", "until", "retries", "delay", "block", "rescue", "always",
    }
    return [k for k in task if k not in directives]


def replace_module(text: str, old_module: str, new_module: str,
                   new_args: dict[str, Any] | None = None) -> str:
    """Swap a module in every task that uses it, keeping the task's other keys.

    ``old_module`` may be short (``apt_key``) or fully qualified
    (``ansible.builtin.apt_key``); both forms are matched.
    """
    data = load(text)
    if not isinstance(data, list):
        raise YamlEditError("expected a playbook (a list of plays)")

    short = old_module.rsplit(".", 1)[-1]
    replaced = 0

    for play in data:
        if not isinstance(play, dict):
            continue
        for section in ("tasks", "pre_tasks", "post_tasks", "handlers"):
            for task in play.get(section) or []:
                if not isinstance(task, dict):
                    continue
                for key in _module_keys(task):
                    if key.rsplit(".", 1)[-1] != short:
                        continue
                    args = task[key]
                    items = list(task.items())
                    task.clear()
                    for tk, tv in items:
                        if tk == key:
                            task[new_module] = (
                                dict(new_args) if new_args is not None else args)
                        else:
                            task[tk] = tv
                    replaced += 1

    if not replaced:
        raise YamlEditError(f"module {old_module!r} not found in playbook")
    return dump(data, explicit_start=has_document_start(text))


def find_module_use(text: str, module: str) -> list[str]:
    """Return the names of tasks using ``module`` (short or FQCN)."""
    try:
        data = load(text)
    except YamlEditError:
        return []
    if not isinstance(data, list):
        return []
    short = module.rsplit(".", 1)[-1]
    names = []
    for play in data:
        if not isinstance(play, dict):
            continue
        for section in ("tasks", "pre_tasks", "post_tasks", "handlers"):
            for task in play.get(section) or []:
                if isinstance(task, dict) and any(
                        k.rsplit(".", 1)[-1] == short for k in _module_keys(task)):
                    names.append(str(task.get("name", "<unnamed task>")))
    return names


def referenced_variables(text: str) -> set[str]:
    """Return every ``{{ var }}`` name referenced in a document."""
    return set(re.findall(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", text))
