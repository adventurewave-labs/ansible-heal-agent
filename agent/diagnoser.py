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
import os
import re
import shutil
import signal
import subprocess
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


def _run_probe(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run``, but a timeout kills the whole process group.

    ``ansible-inventory``/``ansible``/``ansible-doc`` can themselves spawn a
    repository-supplied dynamic-inventory script or plugin as a *child* of
    that process. ``subprocess.run``'s own timeout handling only signals the
    process it started directly — the grandchild, the actual hung script,
    survived every timeout here, reparented to init, one more orphan per
    probe that ran long. ``start_new_session=True`` put the whole tree in its
    own process group for exactly this reason; nothing used to act on it.
    """
    timeout = kwargs.pop("timeout", None)
    proc = subprocess.Popen(argv, start_new_session=True, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


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
    # `fd00::21:10.0.0.5` is BOTH: a syntactically valid IPv6 address with an
    # IPv4-mapped tail, and a union of `fd00::21` and `10.0.0.5`. Ansible reads
    # it as the union. Anything carrying a dot alongside its colons is treated
    # as ambiguous and refused, rather than guessed at.
    if ":" in text and "." in text:
        return (f"{text!r} is ambiguous — it parses both as one IPv6 literal "
                f"and as a union of patterns, and Ansible reads it as the "
                f"union; no rename is proposed")
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


#: ansible-core's own words when a source did not parse. Any of these means the
#: probe saw less than a real run would, so its "that host does not exist" is
#: not an answer — it is the absence of one.
_NO_INVENTORY_RE = re.compile(
    r"No inventory was parsed"
    r"|Unable to parse .*as an inventory source"
    r"|Failed to parse inventory"
    r"|Failed to parse .*with .* plugin"
    r"|skipping inventory source", re.I)


#: True when the operator has asked the agent to commit to this repository, so
#: running that repository's own inventory code is no more than ansible-playbook
#: would do. False in dry-run.
_TRUST_REPO = False


def set_trust_repo(trust: bool) -> None:
    global _TRUST_REPO
    _TRUST_REPO = bool(trust)


#: Suffixes the probe's restricted plugin set can actually read.
_PARSEABLE_SUFFIXES = {".yml", ".yaml", ".ini", ".toml", ""}


def _unparseable_sources_configured() -> bool:
    """Does this repo point Ansible at an inventory source the probe cannot read?

    An executable script, a directory of sources, or a plugin config file. Any
    of those means ansible-core sees hosts the probe does not.
    """
    sources: list[str] = []
    env_inventory = os.environ.get("ANSIBLE_INVENTORY", "")
    if env_inventory:
        sources.extend(env_inventory.split(","))
    try:
        from pipeline.runner import configured_inventory_sources
        sources.extend(configured_inventory_sources())
    except Exception:
        pass
    for raw in sources:
        entry = raw.strip()
        if not entry:
            continue
        path = (repo_root() / entry)
        if path.is_dir():
            return True
        if path.is_file():
            if path.suffix.lower() not in _PARSEABLE_SUFFIXES:
                return True
            if os.access(path, os.X_OK):
                return True
    return False


def inventory_hosts_from_ansible(trust_repo: bool) -> set[str] | None:
    """Every host ansible-core sees, or None if it could not be asked.

    ``trust_repo`` decides whether the repository's own inventory plugins and
    scripts may run. In apply and PR modes the operator has pointed the agent
    at their own repository and asked it to commit there, so executing that
    repository's inventory code is no more than `ansible-playbook` would do. In
    dry-run — the mode for a repository you have not decided to trust — it is
    not, so the restricted parser set applies and a dynamic source simply makes
    the answer unavailable.

    This replaces a series of predicates over filenames, suffixes and exec bits
    that tried to guess which sources were readable and what counted as an
    inventory. Every one of them was defeated by an ordinary spelling it had
    not enumerated: a `.yml` plugin config, a second inventory file, a role
    under playbooks/. Enumerating spellings does not converge; asking
    ansible-core does.
    """
    exe = shutil.which("ansible-inventory")
    if not exe:
        return None
    env = {**os.environ, "ANSIBLE_LOCALHOST_WARNING": "False"}
    if not trust_repo:
        env["ANSIBLE_INVENTORY_ENABLED"] = "yaml,ini,toml,host_list"
        env["ANSIBLE_INVENTORY_PLUGINS"] = str(
            repo_root() / ".ansible-heal-no-plugins")
    # Let ansible.cfg win when it names sources; otherwise point ansible at the
    # convention this agent uses, because ansible's own default is
    # /etc/ansible/hosts and it would otherwise see nothing at all.
    argv = [exe, "--list"]
    try:
        from pipeline.runner import configured_inventory_sources, inventory_path
        if not configured_inventory_sources():
            inv = inventory_path()
            if inv.is_file():
                argv.extend(["-i", str(inv)])
    except Exception:
        pass
    try:
        proc = _run_probe(
            argv, cwd=str(repo_root()), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=60, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    if _NO_INVENTORY_RE.search(proc.stdout + proc.stderr):
        # A source did not parse, so this is a partial view of the hosts. A
        # partial view is not grounds for deleting one of them.
        return None
    try:
        data = json.loads(proc.stdout[proc.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        return None
    meta = data.get("_meta", {}).get("hostvars", {})
    hosts = {str(h) for h in meta}
    for key, value in data.items():
        if key == "_meta" or not isinstance(value, dict):
            continue
        hosts.update(str(h) for h in value.get("hosts", []) or [])
    hosts.discard("localhost")
    return hosts or None


def ansible_resolves(pattern: str) -> bool | None:
    """Does real Ansible match ``pattern`` against the repo's inventory?

    ``True``/``False`` when ansible-core can answer, ``None`` when it is not
    installed or the inventory is unreadable.

    This exists because of a pattern that repeated across six audit rounds. The
    bundled simulator is a reimplementation of Ansible's pattern language, and
    every gap between it and ansible-core turned into a *destructive* bug: the
    simulator failed to match something ordinary, and this diagnoser read "no
    match" as "the inventory is wrong" and renamed a live host. Patching the
    simulator one construct at a time never converged — IPv6, `?` globs,
    whitespace separators, nested groups, exclusion, intersection each shipped
    as a fix and each left a neighbouring case broken.

    So the destructive step now asks the authority instead of trusting the
    simulation. If ansible-core says the pattern resolves, no rename is
    proposed, whatever the simulator thought.
    """
    exe = shutil.which("ansible")
    if not exe:
        return None
    if str(pattern).startswith("-"):
        # Would be read as a flag. Never fail *open* on a parse quirk.
        return None

    # Third-party inventory plugins and inventory scripts are executable code
    # supplied by the repository under audit. --dry-run exists to be pointed at
    # a repository you have not decided to trust, so the probe runs with only
    # the builtin file-based parsers enabled and no plugin path from the repo.
    env = {
        **os.environ,
        "ANSIBLE_LOCALHOST_WARNING": "False",
        # ANSIBLE_INVENTORY_UNPARSED_WARNING is deliberately NOT set: that
        # warning is how the probe tells "parsed your inventory, host absent"
        # from "parsed nothing at all", and only the first authorises deleting
        # a host. Suppressing it made an empty repo look like a definitive no.
        "ANSIBLE_INVENTORY_ENABLED": "yaml,ini,toml,host_list",
        "ANSIBLE_INVENTORY_PLUGINS": str(repo_root() / ".ansible-heal-no-plugins"),
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
    }

    # Two probes. The first lets ansible-core resolve its own inventory exactly
    # as it would for a real run — its ansible.cfg, ANSIBLE_CONFIG,
    # ANSIBLE_INVENTORY, a comma-separated list, all of it. Handing it `-i <our
    # guess>` instead, as this used to, overrode the very thing being asked
    # about: on a repo with two inventory files the probe was shown one of
    # them, answered "no such host" truthfully, and a live host in the other
    # file was renamed away.
    attempts: list[list[str]] = [[exe, str(pattern), "--list-hosts"]]
    inventory = repo_root() / _inventory_rel()
    if inventory.is_file():
        attempts.append([exe, str(pattern), "-i", str(inventory), "--list-hosts"])

    answered = False
    for argv in attempts:
        try:
            proc = _run_probe(
                argv, cwd=str(repo_root()), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=30, env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            continue
        match = re.search(r"hosts \((\d+)\)", proc.stdout)
        if match is None:
            continue
        combined = proc.stdout + proc.stderr
        if _unparseable_sources_configured():
            # The probe deliberately runs without the script, auto and
            # constructed plugins, because those execute code the target repo
            # supplies. A repo that uses one of them therefore shows the probe
            # fewer hosts than a real run has, and "hosts (0)" from a partial
            # view is not evidence that a host is absent — it is how a live
            # host in a dynamic source got renamed away.
            return None
        if _NO_INVENTORY_RE.search(combined):
            # ansible-core exits 0 and prints "hosts (0)" when it parsed no
            # inventory at all — indistinguishable, by the number alone, from
            # "parsed it, that host is absent". Only the second is grounds for
            # deleting a host. This probe disables script/auto/constructed
            # plugins deliberately (they are code the target repo supplies), so
            # a dynamic inventory lands here every time.
            continue
        answered = True
        if int(match.group(1)) > 0:
            return True
    # "No definitive answer" must stay None. Collapsing it to False would let a
    # probe that merely failed to run authorise the rename it exists to block.
    return False if answered else None


def module_resolves(module: str) -> bool | None:
    """Does ansible-core resolve ``module``? None if it cannot be asked.

    The simulator declares `apt_key` removed. It is not — it resolves on 2.19
    — so the agent was rewriting working playbooks autonomously: a key *import*
    became a file *download*, dropping `id:` and `state:`, on a repo where
    nothing was broken. A modernisation is a change the operator chooses, not
    one an autonomous agent applies to a green pipeline.
    """
    exe = shutil.which("ansible-doc")
    if not exe or not module or module.startswith("-"):
        return None
    try:
        proc = _run_probe(
            [exe, "-t", "module", "-j", module], cwd=str(repo_root()),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=30,
            env={**os.environ, "ANSIBLE_DEPRECATION_WARNINGS": "False"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    combined = proc.stdout + proc.stderr
    # ansible-doc reports an unresolvable module two ways, and the exit code
    # alone cannot tell them apart from a genuine answer: it exits 0 for a
    # name it has never heard of (`{}` plus a "was not found" warning), and
    # it exits *1* for a name it tombstoned outright — removed on a
    # deprecation cycle, with a message naming the replacement. Gating on the
    # exit code before the message text treated the second, more definite
    # case as unanswerable (None) rather than False.
    if re.search(rf"{re.escape(module)}.{{0,20}}was not found", combined):
        return False
    if re.search(r"module has been removed\b", combined):
        return False
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout[proc.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        return None
    return bool(doc)


def _host_vars_candidates() -> list[Path]:
    """Every directory Ansible would look for ``host_vars/`` next to, for this repo.

    Ansible discovers ``host_vars/`` relative to wherever an inventory source
    actually lives, not only under the flat ``ansible/`` convention this used
    to hardcode. A repo whose inventory lives at
    ``ansible/inventories/prod/hosts.yml`` keeps its host_vars at
    ``ansible/inventories/prod/host_vars/`` — real, live vars that
    ``ansible-inventory`` genuinely merges in — and the flat-only check never
    looked there, so a rename it called safe orphaned them anyway.
    """
    root = repo_root()
    bases = {root / "ansible"}
    try:
        from pipeline.runner import configured_inventory_sources
        for source in configured_inventory_sources():
            try:
                path = (root / source).resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            bases.add(path if path.is_dir() else path.parent)
    except Exception:
        pass
    try:
        inv = (root / _inventory_rel()).resolve()
        bases.add(inv if inv.is_dir() else inv.parent)
    except (OSError, RuntimeError, ValueError):
        pass
    return sorted(bases)


def _host_vars_file(host: str) -> str | None:
    """The host_vars file defining variables for ``host``, if there is one.

    A rename that leaves it behind silently detaches every variable the host
    had — connection details included — and breaks plays that were working.
    """
    root = repo_root()
    for base in _host_vars_candidates():
        for suffix in (".yml", ".yaml"):
            path = base / "host_vars" / f"{host}{suffix}"
            if path.is_file():
                try:
                    return str(path.relative_to(root))
                except ValueError:
                    return str(path)
    return None


def _add_host_created_names() -> set[str]:
    """Host names any play in this repo creates at runtime via ``add_host``.

    ``add_host`` is an ordinary "provision then target" pattern: an earlier
    play registers a host that exists only for the rest of that run, and
    ``ansible-inventory`` — the authority the rename below otherwise trusts
    completely — has no way to see it, by design. When that runtime name
    happens to be a near-miss for an unrelated, already-defined static host —
    an everyday naming-convention collision, not a contrived one — the
    closest-match rename mistook the real host for a stale spelling of the
    runtime one and renamed it away.
    """
    names: set[str] = set()
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
            for task in play.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                for key in ("add_host", "ansible.builtin.add_host"):
                    block = task.get(key)
                    if isinstance(block, dict):
                        for field in ("name", "hostname"):
                            value = block.get(field)
                            if isinstance(value, str) and value:
                                names.add(value)
                    elif isinstance(block, str):
                        # `add_host: name=foo groups=bar` free-form shape.
                        m = re.search(r"(?:^|\s)(?:name|hostname)=(\S+)", block)
                        if m:
                            names.add(m.group(1))
    return names


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

    # Only part of the inventory is visible. The agent cannot see a dynamic
    # source, a directory of sources, or a plugin config — it deliberately does
    # not execute code the target repo supplies — so it cannot know whether the
    # host it is about to rename away is defined somewhere it cannot read. It
    # renamed a live host out of a static file that a dynamic source also
    # populated, and reported success.
    # The authority on which hosts exist is ansible-core, not this agent's
    # reading of the inventory file. Asking it covers every source the repo
    # configures — a second file, a plugin config, a dynamic script — without
    # the agent having to recognise the shape of any of them.
    known = inventory_hosts_from_ansible(_TRUST_REPO)
    if known is None:
        return _no_fix(
            "the agent could not get a complete host list from ansible-core "
            "for this repository, so it can see only part of your inventory "
            "and will not rename anything in it. Install ansible-core, or run "
            "in apply mode if this repository's inventory plugins are yours to "
            "execute", failure.get("type", "other"))
    if str(expected) in known:
        return _no_fix(
            f"ansible-core already resolves '{expected}' in this inventory, so "
            f"nothing is stale; the bundled simulator failed to match a source "
            f"or a pattern it does not implement")

    # Ask ansible-core before touching anything. A "failure" the simulator
    # invented is not grounds for editing someone's inventory.
    if ansible_resolves(as_written) is True:
        return _no_fix(
            f"ansible-core resolves {as_written!r} to at least one host, so the "
            f"inventory is not stale — the bundled simulator failed to match a "
            f"pattern it does not implement. Re-run with PIPELINE_RUNNER=real "
            f"for an accurate pipeline result")

    if str(expected) in _add_host_created_names():
        return _no_fix(
            f"'{expected}' is created at runtime by an add_host task in this "
            f"repo's playbooks; ansible-inventory cannot see it by design, "
            f"and a static host with a similar name is not evidence that "
            f"'{expected}' is a stale spelling of it")

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

    orphaned = _host_vars_file(stale)
    if orphaned:
        return _no_fix(
            f"renaming '{stale}' would orphan {orphaned}, which defines "
            f"variables for it by name; rename that file too and re-run, or "
            f"make the change deliberately")

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

    # Ask ansible-core, exactly as the host rename does. The simulator declares
    # apt_key removed; ansible-core resolves it, so acting on the simulator here
    # rewrote working playbooks — a key import became a file download, dropping
    # `id:` and `state:`. Migrating a module that still works is a change the
    # operator chooses, not one an autonomous agent makes to a green pipeline.
    if module_resolves(module) is True:
        return _no_fix(
            f"ansible-core resolves '{module}', so the play is not broken. It "
            f"may still be worth modernising, but that is a change to make "
            f"deliberately, not a repair")

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


def _is_inventory_target(target: str) -> bool:
    """Does this path point at an inventory, however it is spelled?

    `./ansible/hosts.yml`, `ansible//inventory.yml` and a second inventory file
    under `ansible/inventory/` all reach the patcher, which normalises them —
    so a comparison against one exact string let a rewrite through unchecked.
    Resolving both sides on the filesystem, rather than comparing strings,
    also covers a symlinked inventory and an absolute path in a second
    `ansible.cfg` source, and treats a *directory*-configured inventory
    (`inventory = ansible/inventories/`) as covering every file inside it —
    ansible-core reads the whole directory as one inventory, and a
    string-exact comparison only ever matched the directory's own path.
    """
    if not target:
        return False
    try:
        target_abs = (repo_root() / target).resolve()
    except (OSError, RuntimeError, ValueError):
        return False

    def matches(source_raw: str) -> bool:
        try:
            source_abs = (repo_root() / source_raw).resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        if source_abs == target_abs:
            return True
        try:
            is_dir = source_abs.is_dir()
        except OSError:
            is_dir = False
        if is_dir:
            try:
                target_abs.relative_to(source_abs)
                return True
            except ValueError:
                return False
        return False

    if matches(_inventory_rel()):
        return True
    # Every source the repo configures counts, not just the first — a
    # filename heuristic counted the wrong things in both directions: it
    # missed a second inventory file, and it refused legitimate fixes to
    # `playbooks/bastion-hosts.yml` and `inventories/prod/group_vars/all.yml`
    # on the strength of a substring.
    try:
        from pipeline.runner import configured_inventory_sources
        for source in configured_inventory_sources():
            if matches(source):
                return True
    except Exception:
        pass
    return False


def _guard_inventory_fix(failure: dict, diagnosis: dict[str, Any]) -> dict[str, Any]:
    """Apply the host-rename guards to a diagnosis from any source.

    These checks used to live inside ``fallback_diagnose`` only, so a fix that
    came from the LLM reached the patcher having passed none of them — and the
    LLM is the default path. The prompt template asks the model, in as many
    words, to "rename the host in the inventory to the name the playbook
    targets", which is precisely the destructive act every one of these guards
    exists to prevent. The patcher's own gates (allowlist, traversal, vault,
    YAML validity) do not look at Ansible semantics at all.
    """
    fix = diagnosis.get("fix") or {}
    if fix.get("action", "none") == "none":
        return diagnosis

    target = str(fix.get("target_file") or "")
    if not _is_inventory_target(target) and fix.get("action") != "rename_host":
        return diagnosis

    # An `edit_file` fix — the shape PROMPT_TEMPLATE actually asks the model for
    # — carries `search`/`replace`, not `old`/`new`. Every content check below
    # reads old/new, so for the shape the agent itself requests they all
    # short-circuited and an arbitrary string replacement landed on the
    # inventory: a host nothing had diagnosed was deleted as collateral inside
    # a replace span, and the guard approved it. A free-text rewrite of an
    # inventory is not something this agent can validate, so it is not allowed
    # to make one.
    if fix.get("action") not in ("rename_host", "none"):
        return _no_fix(
            f"a {fix.get('action')!r} fix targeting {target} would rewrite the "
            f"inventory as free text; only a checked host rename is permitted "
            f"there", failure.get("type", "other"))

    as_written = failure.get("raw_pattern") or failure.get("pattern") or failure.get("host")
    if not as_written:
        return diagnosis

    known = inventory_hosts_from_ansible(_TRUST_REPO)
    if known is None:
        return _no_fix(
            "the agent could not get a complete host list from ansible-core, "
            "so it will not rename anything in this inventory",
            failure.get("type", "other"))
    if str(as_written) in known:
        return _no_fix(
            f"ansible-core already resolves '{as_written}'; nothing is stale",
            failure.get("type", "other"))

    reason = _not_a_single_hostname(str(as_written))
    if reason:
        return _no_fix(reason, failure.get("type", "other"))
    if ansible_resolves(str(as_written)) is True:
        return _no_fix(
            f"ansible-core resolves {as_written!r} to at least one host, so the "
            f"inventory is not stale; no rename is proposed",
            failure.get("type", "other"))

    inventory = _read(_inventory_rel())
    if inventory is not None:
        try:
            groups = yaml_edit.inventory_groups(inventory)
            hosts = yaml_edit.inventory_hosts(inventory)
        except Exception:
            return diagnosis
        if str(as_written) in groups:
            return _no_fix(
                f"'{as_written}' is a group in {_inventory_rel()}, not a host; "
                f"renaming a host to match it would create a name collision",
                failure.get("type", "other"))
        old, new = fix.get("old"), fix.get("new")
        if new and str(new) in hosts and str(new) != str(old):
            # Refuse here rather than letting the rename fail inside the
            # patcher: the operator gets the reason, not a patch error.
            return _no_fix(
                f"'{new}' is already a host in {_inventory_rel()}; renaming "
                f"'{old}' onto it would delete one of them",
                failure.get("type", "other"))
        if new and str(new) in _add_host_created_names():
            return _no_fix(
                f"'{new}' is created at runtime by an add_host task in this "
                f"repo's playbooks; ansible-inventory cannot see it by "
                f"design, and renaming '{old}' onto it would guess that it "
                f"is a stale spelling rather than an unrelated host",
                failure.get("type", "other"))
        # This guard used to end here, so a rename proposed by the LLM never
        # went through the same host_vars orphan check the deterministic
        # path applies below — the exact damage class that check exists for,
        # unguarded on the default path.
        if old:
            orphaned = _host_vars_file(str(old))
            if orphaned:
                return _no_fix(
                    f"renaming '{old}' would orphan {orphaned}, which "
                    f"defines variables for it by name; rename that file "
                    f"too and re-run, or make the change deliberately",
                    failure.get("type", "other"))
        claimed_by = _play_host_patterns().get(str(old)) if old else None
        if claimed_by:
            return _no_fix(
                f"{claimed_by} still targets '{old}'; renaming it would break "
                f"that play", failure.get("type", "other"))
    return diagnosis


def _guard_module_fix(failure: dict, diagnosis: dict[str, Any]) -> dict[str, Any]:
    """Apply the module-migration guard to a diagnosis from any source.

    Mirrors ``_guard_inventory_fix``: PROMPT_TEMPLATE asks the model, for a
    `removed_module` failure, to "replace the task's module with its modern
    equivalent" via an `edit_file` search/replace — free text this agent
    cannot audit for what else it might touch, and with no check at all that
    the module it names actually needs replacing. That let an LLM-proposed
    fix rewrite a task using `apt_key`, which still resolves on ansible-core
    2.19, exactly the regression asking ansible-doc was supposed to have
    closed — the check existed only on the deterministic path.
    """
    fix = diagnosis.get("fix") or {}
    action = fix.get("action", "none")
    if action == "none":
        return diagnosis
    if failure.get("type") != "removed_module" and action != "replace_module":
        return diagnosis

    if action == "edit_file":
        return _no_fix(
            f"a {action!r} fix for a removed-module failure would rewrite "
            f"the playbook as free text; only a checked module replacement "
            f"is permitted there", failure.get("type", "other"))
    if action != "replace_module":
        return diagnosis

    old_module = (fix.get("old_module") or failure.get("module")
                  or failure.get("module_short"))
    if old_module and module_resolves(str(old_module)) is True:
        return _no_fix(
            f"ansible-core resolves '{old_module}', so the play is not "
            f"broken; migrating a module that still works is a change to "
            f"make deliberately, not a repair", failure.get("type", "other"))
    return diagnosis


def diagnose(failure: dict, use_llm: bool = True) -> dict[str, Any]:
    """Try the LLM first, fall back to the deterministic rules on any error."""
    if not use_llm:
        return fallback_diagnose(failure)
    try:
        result = llm_diagnose(failure)
        if "fix" not in result or "target_file" not in result.get("fix", {}):
            raise ValueError("LLM diagnosis missing required keys")
        guarded = _guard_inventory_fix(failure, result)
        guarded = _guard_module_fix(failure, guarded)
        return guarded
    except Exception as e:  # noqa: BLE001
        fb = fallback_diagnose(failure)
        fb["_fallback_reason"] = str(e)
        return fb
