"""Mock ansible-playbook runner.

Parses the in-repo playbooks and inventory, walks the tasks, and emits log
lines in the *shape* of Ansible output for three failure classes:

  1. Stale hostname       → "UNREACHABLE! fatal: [web-server-01]: UNREACHABLE! ..."
  2. Removed module       → "ERROR! couldn't resolve module action 'apt_key' ..."
  3. Undefined variable   → "fatal: [web-01]: FAILED! => msg=The task includes an
                              option with an undefined variable 'nginx_port'."

Where this simulator and real ansible-core diverge — read this before trusting
a mock log as evidence of real behaviour:

* A host pattern matching nothing is reported here as ``UNREACHABLE`` with a
  nonzero exit. Real ansible-core warns and exits **0**, which is precisely why
  ``pipeline/callback_plugins/heal_json.py`` exists. ``run_real`` classifies it
  as ``no_hosts_matched``.
* ``apt_key`` is treated here as unresolvable so the seeded scenario has a
  module class to exercise. On ansible-core 2.19 it still resolves — it is
  deprecated, not removed — so the replacement the agent proposes for it is a
  modernisation. ``docker`` is the module that genuinely does not resolve, and
  it is what ``tests/test_real_ansible.py`` uses against the real binary.

Exit codes this runner emits:
  0  → success
  2  → one or more failures detected

Real ansible-core also uses 4 for a parse-time error, which this runner never
produces; ``agent.log_scanner`` handles that case from real output.

The runner writes a timestamped log to ``pipeline/runs/run-<ts>.log`` and
also returns the log path so the agent can pick it up immediately.
"""

from __future__ import annotations

import configparser
import ipaddress
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


def _log_ref(log_path: Path) -> str:
    """How a run log is referred to in a RunResult.

    Repo-relative when the log lives inside the repo, which is the normal case
    and keeps transcripts portable. Dry-run redirects logs to a scratch
    directory outside the repo (so that "writes nothing" is true), and there
    the absolute path is the only thing that resolves.
    """
    try:
        return str(log_path.relative_to(repo_root()))
    except ValueError:
        return str(log_path)


class InputUnreadable(RuntimeError):
    """A file the run needs is missing or is not parseable YAML."""


def _load_yaml(path: Path, expect: str = "mapping"):
    """Load a YAML mapping, or raise InputUnreadable.

    Previously this let FileNotFoundError and yaml.YAMLError escape to the CLI
    as a traceback, so a repo missing ``group_vars/all.yml`` — or carrying one
    typo — exited 1 with a stack trace instead of a reported failure.
    """
    try:
        if path.is_file():
            head = path.open("rb").read(len(b"$ANSIBLE_VAULT"))
            if head == b"$ANSIBLE_VAULT":
                # Say what it is. "not a mapping" is true but useless: the
                # operator needs to know the agent found encrypted content and
                # will not touch it.
                raise InputUnreadable(
                    f"{_rel(path)} is ansible-vault encrypted; the agent has no "
                    f"vault password and will not read or rewrite it")
        if path.exists() and not path.is_file():
            # Opening a FIFO blocks until a writer appears — forever, in
            # practice. A device node is no better.
            raise InputUnreadable(
                f"{_rel(path)} is not a regular file")
        with path.open() as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as e:
        raise InputUnreadable(f"{_rel(path)} does not exist") from e
    except OSError as e:
        raise InputUnreadable(f"{_rel(path)} could not be read: {e}") from e
    except yaml.YAMLError as e:
        raise InputUnreadable(f"{_rel(path)} is not valid YAML: {e}") from e
    if data is None:
        return [] if expect == "list" else {}
    if expect == "list":
        # A playbook is a list of plays. A mapping is a single play, which
        # Ansible also accepts.
        if isinstance(data, dict):
            return [data]
        if not isinstance(data, list):
            raise InputUnreadable(
                f"{_rel(path)} is a YAML {type(data).__name__}, not a list of plays")
        return data
    if not isinstance(data, dict):
        # A list parses as YAML but every caller of an inventory or a vars file
        # indexes it as a mapping. This used to escape as AttributeError or
        # TypeError from deep inside the run.
        raise InputUnreadable(
            f"{_rel(path)} is a YAML {type(data).__name__}, not a mapping")
    return data


#: Where group_vars may live. Kept in step with agent.diagnoser, which offers
#: the same three; the runner used to hard-code all.yml and die without it.
def configured_inventory() -> Path | None:
    """The inventory path from ansible.cfg, if it names one.

    Both runners hardcoded ansible/inventory.yml, so a repo whose ansible.cfg
    points elsewhere — the normal way to lay one out — was reported as having
    no inventory at all.
    """
    cfg = repo_root() / "ansible.cfg"
    if not cfg.is_file():
        return None
    parser = configparser.ConfigParser(allow_no_value=True, strict=False)
    try:
        parser.read(cfg)
    except configparser.Error:
        return None
    for section in ("defaults", "inventory"):
        if parser.has_option(section, "inventory"):
            raw = (parser.get(section, "inventory") or "").split(",")[0].strip()
            if raw:
                return (repo_root() / raw).resolve()
    return None


def inventory_path() -> Path:
    """Where the inventory lives: ansible.cfg's answer, else the default."""
    return configured_inventory() or (repo_root() / "ansible" / "inventory.yml")


_GROUP_VARS_CANDIDATES = (
    "ansible/group_vars/all.yml",
    "ansible/group_vars/all.yaml",
    "ansible/group_vars/all/main.yml",
)


def _load_narrow_vars() -> dict[str, str]:
    """Variables defined only for *some* hosts, mapped to where they came from.

    Merging these into one namespace let the simulator call a pipeline healthy
    when a variable was defined for one host and undefined for its neighbour.
    They are tracked separately so the run can say "defined in X, but not for
    every host this play targets" and decline, rather than guess either way.
    """
    narrow: dict[str, str] = {}
    for directory in ("ansible/group_vars", "ansible/host_vars"):
        base = repo_root() / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not (path.is_file() and path.suffix in (".yml", ".yaml")):
                continue
            if path.name in ("all.yml", "all.yaml") and path.parent == base:
                continue
            try:
                data = _load_yaml(path)
            except InputUnreadable:
                continue
            for name in data:
                narrow.setdefault(str(name), _rel(path))
    return narrow


def _load_group_vars() -> dict:
    """Every variable Ansible would see from group_vars and host_vars.

    Only ``group_vars/all*`` used to be read, so a variable defined in
    ``group_vars/webservers.yml`` or ``host_vars/web-01.yml`` looked undefined
    and the agent committed a fabricated default for it. Those are false
    failures with a junk commit attached, even though Ansible's precedence
    means the operator's value still won at runtime.

    Merged shallowly and only to answer "is this name defined anywhere?" — the
    agent does not attempt to model Ansible's precedence, and does not need to.
    """
    merged: dict = {}
    for rel in _GROUP_VARS_CANDIDATES:
        path = repo_root() / rel
        if path.is_file():
            merged.update(_load_yaml(path))
            break
    return merged


#: A log line per host per task is fine for a demo inventory and absurd for a
#: real one — 5,000 hosts x 500 tasks produced a 42 MB file per run, written
#: into the operator's repository. Beyond this many hosts the line is summarised.
_MAX_HOSTS_LOGGED = 20


def _host_lines(kind: str, hosts: list[str], template: str = "ok: [{h}]") -> str:
    shown = hosts[:_MAX_HOSTS_LOGGED]
    parts = [template.format(h=h) for h in shown]
    if len(hosts) > _MAX_HOSTS_LOGGED:
        parts.append(f"... and {len(hosts) - _MAX_HOSTS_LOGGED} more host(s)")
    sep = "  " if kind == "recap" else "\n"
    return sep.join(parts)


def _rel(path: Path) -> str:
    """``path`` relative to the repo when possible, for readable messages."""
    try:
        return str(path.relative_to(repo_root()))
    except ValueError:
        return str(path)


def _plays_tasks(play: dict, source: str) -> list[dict]:
    """A play's tasks, or a clear error. Ansible rejects both shapes below."""
    tasks = play.get("tasks", [])
    if tasks is None:
        return []
    if not isinstance(tasks, list):
        raise InputUnreadable(
            f"{source}: `tasks:` is a {type(tasks).__name__}, not a list")
    for task in tasks:
        if not isinstance(task, dict):
            raise InputUnreadable(
                f"{source}: a task is a {type(task).__name__}, not a mapping")
    return tasks


def _expand_playbook(playbook_path: Path, _seen: frozenset[Path] = frozenset()) -> list[dict]:
    """Walk a playbook file and expand ``import_playbook`` directives.

    Returns a flat list of play dicts (each one already has ``hosts:`` etc.).
    Each returned play is annotated with its source playbook so failures can be
    attributed to the right file.
    """
    # Resolve to absolute so relative_to(repo_root()) works regardless of cwd.
    playbook_path = playbook_path.resolve()
    if playbook_path in _seen:
        # A playbook that imports itself, or two that import each other, used to
        # recurse until Python gave up with a RecursionError.
        raise InputUnreadable(
            f"{_rel(playbook_path)} is part of an import_playbook cycle")
    try:
        playbook_path.relative_to(repo_root().resolve())
    except ValueError:
        # Writes are traversal-checked; reads were not, so an import could pull
        # in — and parse — any file on the machine.
        raise InputUnreadable(
            f"import_playbook target {playbook_path} is outside the repository"
        ) from None
    _seen = _seen | {playbook_path}
    raw = _load_yaml(playbook_path, expect="list")

    expanded: list[dict] = []
    for entry in raw:
        if isinstance(entry, dict) and "import_playbook" in entry:
            sub_path = playbook_path.parent / entry["import_playbook"]
            for sub in _expand_playbook(sub_path, _seen):
                expanded.append(sub)
        elif isinstance(entry, dict):
            entry["_source_playbook"] = str(playbook_path.relative_to(repo_root()))
            expanded.append(entry)
    return expanded



def _pattern_parts(pattern) -> list[str] | None:
    """Split a ``hosts:`` value into the sub-patterns Ansible would union.

    Accepts a string (optionally comma-separated) or a YAML list — both are
    valid Ansible and both used to break this runner: a list raised
    ``TypeError: unhashable type: 'list'``, and a comma-separated string
    matched nothing, which the diagnoser then "fixed" by renaming an inventory
    host to the literal pattern string, breaking a repo that had been green.

    Returns ``None`` if any part uses syntax this simulator cannot evaluate.
    """
    if isinstance(pattern, (list, tuple)):
        raw = [str(p) for p in pattern]
    elif pattern is None:
        # `hosts:` present but empty. Ansible rejects this outright rather than
        # matching nothing; str(None) used to produce a pattern of "None".
        return None
    else:
        raw = [str(pattern)]
    # Ansible separates patterns on commas, semicolons AND whitespace. Handling
    # only the comma meant `hosts: web-01 db-01` matched nothing, and the
    # diagnoser renamed a host to that literal string.
    parts: list[str] = []
    for item in raw:
        parts.extend(re.split(r"[,;\s]+", item))
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        return None
    # Ranges (`web-0[1:3]`) are checked before the ":" split, which would
    # otherwise cut them in half and hide them.
    if any("[" in p or "]" in p for p in parts):
        return None
    expanded: list[str] = []
    for part in parts:
        expanded.extend(_split_unions(part))
    return expanded or None


def _looks_like_ipv6(text: str) -> bool:
    try:
        ipaddress.IPv6Address(text.strip("[]"))
    except ValueError:
        return False
    return True


def _split_unions(part: str) -> list[str]:
    """Split on ":" the way Ansible does — never through an IPv6 literal.

    Ansible unions patterns on ":", but it is IPv6-aware. Splitting blindly
    turned the host `fd00::21` into the tokens `fd00` and `21`, and the agent
    then renamed a real address to the fragment `fd00` and reported success.
    """
    if _looks_like_ipv6(part):
        return [part]
    return [p for p in part.split(":") if p]


def _inventory_index(inv: dict) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Return ``(hosts, groups)`` for an inventory tree.

    Groups are collected from both the nested ``children:`` form and the flat
    form (``webservers: {hosts: ...}`` directly under ``all``). Only the nested
    form used to register, so a group pattern against a flat inventory looked
    like a missing host on a repo real Ansible runs clean.
    """
    flat: dict[str, dict] = {}
    groups: dict[str, list[str]] = {}
    #: parent -> child group names, for children defined elsewhere in the file.
    _group_refs: dict[str, list[str]] = {}

    def collect(node, group_name: str | None) -> list[str]:
        """Return every host reachable from ``node``, registering groups.

        Returns the transitive host list so a parent group gets its children's
        hosts too. Collecting only ``gv["hosts"]`` meant a parent group like
        `prod` (children: webservers, dbservers) resolved to zero hosts — and
        the agent then "fixed" that by renaming a host to the group's name.
        """
        if not isinstance(node, dict):
            return []
        mine: list[str] = []
        own = node.get("hosts")
        if isinstance(own, dict):
            for host, meta in own.items():
                flat.setdefault(str(host), meta if isinstance(meta, dict) else {})
                mine.append(str(host))
        children = node.get("children")
        if isinstance(children, dict):
            for child_name, child in children.items():
                mine.extend(collect(child, str(child_name)))
                if group_name is not None:
                    _group_refs.setdefault(group_name, []).append(str(child_name))
        # Deliberately NOT recursing into other keys. Real ansible-core skips
        # them — "Skipping unexpected key (webservers) in group (all), only
        # vars, children and hosts are valid" — and resolves such a group to
        # zero hosts. A previous round added "flat group" support on the belief
        # that this shape was common and being mishandled; it is neither.
        # Inventing hosts here made the simulator report a green run where real
        # Ansible skips the play entirely.
        if group_name is not None:
            groups.setdefault(group_name, [])
            groups[group_name].extend(mine)
        return mine

    root = inv.get("all", inv)
    all_hosts = collect(root, "all" if "all" in inv else None)
    groups.setdefault("all", list(dict.fromkeys(all_hosts)))

    # Group names are global in Ansible: `prod: {children: {webservers: {}}}`
    # refers to the same `webservers` defined elsewhere in the file. Resolving
    # by tree position alone left such a parent with zero hosts.
    for _ in range(len(groups)):
        changed = False
        for parent, members in list(groups.items()):
            for name in list(members):
                if name in groups and name != parent:
                    for host in groups[name]:
                        if host not in members:
                            members.append(host)
                            changed = True
        if not changed:
            break
    for parent, refs in list(_group_refs.items()):
        for ref in refs:
            for host in groups.get(ref, []):
                if host not in groups.setdefault(parent, []):
                    groups[parent].append(host)

    return flat, {g: list(dict.fromkeys(h)) for g, h in groups.items()}


def _hosts_in_inventory(inv: dict, pattern) -> list[str] | None:
    """Concrete host names matching an Ansible ``hosts:`` pattern.

    Supports a literal hostname, a group name (nested or flat), ``all``, a
    ``*`` wildcard, a comma-separated union, and a YAML list of any of those.
    Returns ``None`` — meaning "this simulator cannot evaluate the pattern" —
    rather than an empty list, so the caller never mistakes an unimplemented
    pattern for a stale inventory entry.
    """
    parts = _pattern_parts(pattern)
    if parts is None:
        return None

    flat, groups = _inventory_index(inv)

    def resolve(term: str) -> list[str]:
        if term == "localhost" and term not in flat:
            # Ansible always provides an implicit localhost. A play targeting it
            # is not evidence of a stale inventory entry.
            return [term]
        if term == "all":
            return list(flat.keys())
        if term in flat:
            return [term]
        if term in groups:
            return list(groups[term])
        # Globs and regexes match *group* names as well as host names in real
        # Ansible. Matching only hosts made `prod*` look like a missing host on
        # a repo that runs clean.
        if term.startswith("~"):
            try:
                regex = re.compile(term[1:])
            except re.error:
                return []
            hits = [h for h in flat if regex.search(h)]
            for g, members in groups.items():
                if regex.search(g):
                    hits.extend(members)
            return list(dict.fromkeys(hits))
        if any(c in term for c in "*?"):
            pattern = re.escape(term).replace(r"\*", ".*").replace(r"\?", ".")
            regex = re.compile("^" + pattern + "$")
            hits = [h for h in flat if regex.match(h)]
            for g, members in groups.items():
                if regex.match(g):
                    hits.extend(members)
            return list(dict.fromkeys(hits))
        return []

    # Ansible semantics: bare terms union, `!term` excludes, `&term` intersects.
    matched: list[str] = []
    excluded: set[str] = set()
    intersections: list[set[str]] = []
    for part in parts:
        if part.startswith("!"):
            excluded.update(resolve(part[1:]))
        elif part.startswith("&"):
            intersections.append(set(resolve(part[1:])))
        else:
            matched.extend(resolve(part))

    result = [h for h in dict.fromkeys(matched) if h not in excluded]
    for keep in intersections:
        result = [h for h in result if h in keep]
    return result


def _check_removed_modules(task: dict) -> str | None:
    """Return error string if the task uses a removed module."""
    # Simulated only. See the module docstring: on ansible-core 2.19 apt_key
    # still resolves, so a real run would not produce this error for it.
    REMOVED = {
        "apt_key": "The 'apt_key' module is deprecated. Use "
                   "ansible.builtin.get_url to fetch the key into "
                   "/usr/share/keyrings instead.",
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
    succeeded_set: set[str] = set()

    # Resolve playbook_path to absolute so relative_to(repo_root()) works from any cwd.
    playbook_path = playbook_path.resolve()
    inv = _load_yaml(inventory_path())
    group_vars = _load_group_vars()
    narrow_vars = _load_narrow_vars()
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
        tasks = _plays_tasks(play, play_source)
        play_vars = play.get("vars") if isinstance(play.get("vars"), dict) else {}

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
                    f"ERROR! couldn't resolve module action '{f['module']}'."
                )
                continue

            task_vars = task.get("vars") if isinstance(task.get("vars"), dict) else {}
            defined = {**group_vars, **play_vars, **task_vars}

            # Undefined-var check (template vars).
            #
            # `defined` is group_vars plus anything the play or the task sets
            # itself. Checking group_vars alone meant a variable the operator
            # had set in a play-level `vars:` block looked undefined, and the
            # agent committed a fabricated default for it into group_vars —
            # silently overriding a value the operator had chosen.
            refs = _collect_template_vars(task)
            undefined = [r for r in refs if r not in defined]
            if undefined:
                rc = 2
                var = undefined[0]
                if var in narrow_vars:
                    # Defined, but only for some hosts. Adding a global default
                    # would change behaviour for every other host, and calling
                    # the run green would hide a play that fails on a neighbour.
                    failures.append({
                        "type": "narrowly_defined_variable",
                        "host": target if isinstance(target, str) else str(target),
                        "variable": var,
                        "message": (
                            f"'{var}' is defined in {narrow_vars[var]} but not for "
                            f"every host this play targets; the agent will not add "
                            f"a global default over a per-host value"),
                        "playbook": play_source,
                        "play": play_name,
                        "task": task_name,
                    })
                    continue
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

    # ── Phase B: runtime execution ──────────────────────────────────────
    for play in playbook:
        target = play.get("hosts", "all")
        play_name = play.get("name", "<unnamed play>")
        play_source = play.get("_source_playbook", str(playbook_path.relative_to(repo_root())))
        tasks = _plays_tasks(play, play_source)

        hosts = _hosts_in_inventory(inv, target)

        log_lines.append(f"PLAY [{play_name}] *********************************************")

        if hosts is None:
            # The simulator cannot evaluate this pattern. Reporting it as a
            # missing host would be a lie about the repository, and the
            # diagnoser would act on that lie by renaming an inventory entry.
            rc = 2
            failures.append({
                "type": "unsupported_pattern",
                "host": None,
                "pattern": str(target),
                "message": (f"the bundled simulator cannot evaluate the host pattern "
                            f"{target!r}; run with PIPELINE_RUNNER=real to have "
                            f"ansible-playbook resolve it"),
                "playbook": play_source,
                "play": play_name,
            })
            log_lines.append(
                f"ERROR! simulator cannot evaluate host pattern {target!r}")
            log_lines.append("")
            continue

        log_lines.append(f"TASK [target hosts pattern '{target}'] resolved to {len(hosts)} host(s)")

        if not hosts:
            # Pattern understood, matched nothing — a genuinely stale entry.
            rc = 2
            parts = _pattern_parts(target) or [str(target)]
            for part in parts:
                fake_host = part if not part.startswith(("all", "*")) else "<unknown>"
                failures.append({
                    "type": "unreachable_host",
                    "host": fake_host,
                    "pattern": part,
                    # The pattern as the operator wrote it. The diagnoser's
                    # "is this one stale hostname?" guard has to see this, not
                    # the post-split fragment — checking the fragment meant a
                    # pattern the runner had already mangled sailed through.
                    "raw_pattern": target if isinstance(target, str) else str(target),
                    "message": f"UNREACHABLE! fatal: [{fake_host}]: UNREACHABLE! "
                               f"Host '{fake_host}' not found in inventory.",
                    "playbook": play_source,
                    "play": play_name,
                })
                log_lines.append(
                    f"fatal: [{fake_host}]: UNREACHABLE! Host '{fake_host}' not found "
                    f"in inventory. Pattern '{part}' matched 0 hosts."
                )
                log_lines.append(
                    f"PLAY RECAP: {fake_host} : ok=0  changed=0  unreachable=1  failed=0")
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

            # Success path. `succeeded_set` mirrors `succeeded` purely for
            # membership: the list scan made this O(tasks x hosts^2), which on
            # a 5,000-host inventory was ~100s of CPU and a 42 MB log.
            for h in hosts:
                if h not in succeeded_set:
                    succeeded_set.add(h)
                    succeeded.append(h)
            log_lines.append(_host_lines("ok", hosts))
            log_lines.append("")

        log_lines.append(
            "PLAY RECAP: " + _host_lines(
                "recap", hosts, "{h} : ok=1  changed=0  unreachable=0  failed=0"
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
        log_path=_log_ref(log_path),
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
    must not both be reported. Two records are the same failure when they would
    produce the same fix, so identity is keyed on whatever that class's fix is
    keyed on — not on a single fixed tuple. A flat (type, host, ...) key missed
    the undefined-variable case entirely: the text scan's record carries no
    host, so it never collapsed against the callback's, and the operator got two
    identical proposals and a spurious "already defined" patch failure.
    """
    def key(f: dict) -> tuple:
        t = f.get("type")
        if t == "undefined_variable":
            # One definition in group_vars fixes it for every host.
            return (t, f.get("variable"))
        if t == "removed_module":
            # One module swap fixes it wherever the module is used.
            return (t, f.get("module_short") or f.get("module"))
        if t in ("no_hosts_matched", "unreachable_host"):
            return (t, f.get("pattern") or f.get("host"))
        return (t, f.get("host"), f.get("task"))

    # Collapse `primary` against itself first. The callback emits one record
    # per failing *host*, so an undefined variable on N hosts produced N
    # identical proposals and N-1 spurious "already defined" patch failures —
    # the symptom de-duplication exists to prevent, surviving inside one source.
    merged: list[dict] = []
    seen: set[tuple] = set()
    for f in primary:
        k = key(f)
        if k in seen:
            continue
        seen.add(k)
        merged.append(f)
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

    inventory = inventory_path()
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

    text_failures = log_scanner.extract_failures(log_path, text_only=True)
    if sidecar.exists():
        # The callback ran, so it is authoritative for no_hosts_matched: it
        # fires only when a play matched *nothing*. Ansible prints the same
        # WARNING for each unmatched entry of a multi-part pattern even when
        # the play ran fine on the others, and scraping that turned a green run
        # into a destructive rename of a host that was working.
        text_failures = [f for f in text_failures
                         if f.get("type") != "no_hosts_matched"]
    failures = _merge_failures(failures, text_failures)

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
        log_path=_log_ref(log_path),
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
    try:
        if os.environ.get("PIPELINE_RUNNER", "mock") == "real":
            return run_real(playbook_path or default_pb, run_id)
        return run(playbook_path or default_pb, run_id)
    except InputUnreadable as e:
        # A repo the agent cannot read is a finding about the repo, not a crash.
        # This used to surface as a Python traceback and exit 1.
        return RunResult(
            exit_code=2,
            log_path="",
            failures=[{
                "type": "unreadable_input",
                "host": None,
                "message": str(e),
            }],
            succeeded_hosts=[],
        )


if __name__ == "__main__":
    _default = repo_root() / "ansible" / "playbooks" / "site.yml"
    pb = Path(sys.argv[1]) if len(sys.argv) > 1 else _default
    result = run_pipeline(pb)
    print(f"\nexit_code={result.exit_code}")
    print(f"log_path={result.log_path}")
    print(f"failures={len(result.failures)}")
    sys.exit(result.exit_code)
