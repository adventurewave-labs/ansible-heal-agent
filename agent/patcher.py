"""Patcher — apply a diagnosis's fix to the target repository.

Four actions:

``edit_file``      bounded string replace (what an LLM proposes)
``set_yaml_key``   add a top-level key to a YAML document
``rename_host``    rename a host in an inventory
``replace_module`` swap a module in every task that uses it

The last three are *structural*: they edit the parsed document via
``agent.yaml_edit`` and re-serialise, so they work on any file with the right
shape instead of requiring a literal anchor string to be present. That is what
lets the deterministic diagnoser handle a variable it has never seen.

Three gates run before anything is written, in this order:

1. **Path allowlist** (PRD NFR-2). The target must match one of
   ``ANSIBLE_HEAL_ALLOWED_PATHS`` (default ``ansible/**``). Without this an
   LLM-proposed ``target_file`` of ``agent/core.py`` would be applied — the
   agent could rewrite itself.
2. **Traversal check.** The resolved path must stay inside the repo root.
3. **YAML validation.** The patched content must still parse. If it does not,
   nothing is written and PatchError is raised so the caller can fall back.

``dry_run=True`` runs all three gates and returns the diff without writing.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from agent import yaml_edit
from agent.config import ConfigError, allowed_paths, is_path_allowed, repo_root, resolve_in_repo


class PatchError(RuntimeError):
    pass


class PathNotAllowed(PatchError):
    """Raised when a fix targets a file outside the configured write surface."""


#: First line of an ansible-vault file. The ciphertext that follows is a valid
#: YAML *scalar*, so a YAML-validity check alone happily passes it — and a
#: string-replace edit will then write plaintext over an operator's secrets and
#: commit the result. There is no version of "patch it anyway" that is correct
#: here: the agent has no vault password and no business having one.
_VAULT_HEADER = "$ANSIBLE_VAULT"

#: An inline encrypted value: `password: !vault |`. Anchored so the literal
#: string in a comment or a plain scalar does not trip it.
_VAULT_TAG_RE = re.compile(r"(^|:\s+|^\s*-\s+)!vault(\s|\||$)", re.M)


class VaultRefused(PatchError):
    """Raised when a target is ansible-vault encrypted."""


def _refuse_if_vault(path: Path, original: str, target_rel: str) -> None:
    stripped = original.lstrip("\ufeff \t\r\n")
    if stripped.startswith(_VAULT_HEADER):
        raise VaultRefused(
            f"refusing to write {target_rel}: it is ansible-vault encrypted. "
            f"Decrypt it, re-run, and re-encrypt — the agent will not write "
            f"plaintext over a secrets file.")
    if _VAULT_TAG_RE.search(original):
        raise VaultRefused(
            f"refusing to write {target_rel}: it contains inline !vault values. "
            f"A round-trip edit would not preserve them.")


def _validate_yaml(path: Path, content: str) -> None:
    """Raise PatchError if ``content`` is not valid YAML for the given path."""
    if path.suffix not in (".yml", ".yaml"):
        return
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise PatchError(f"patched {path.name} is invalid YAML: {e}") from e


def _write_atomically(target: Path, content: str) -> None:
    """Write via a temp file in the same directory, then rename.

    ``Path.write_text`` truncates first. A write that fails part-way — ENOSPC,
    a quota, EFBIG — therefore leaves the operator with a file cut off mid-key
    that may still parse as YAML, so nothing downstream notices. os.replace is
    atomic within a filesystem, so the target is either the old file or the new
    one and never a prefix of the new one.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if target.exists():
            shutil.copymode(target, tmp)
        else:
            os.chmod(tmp, 0o644)  # mkstemp's 0600 is wrong for a repo file
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def check_allowed(target_rel: str | Path, note: str | None = None) -> None:
    """Raise PathNotAllowed if ``target_rel`` is outside the write surface."""
    if not is_path_allowed(target_rel):
        what = note or str(target_rel)
        raise PathNotAllowed(
            f"refusing to write {what}: outside the allowed write surface "
            f"{list(allowed_paths())}. Set ANSIBLE_HEAL_ALLOWED_PATHS to widen it."
        )


def apply_fix(fix: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    """Apply ``fix`` to the target repo and return a result record.

    With ``dry_run=True`` every check still runs and the diff is still computed,
    but the file is left untouched and ``["applied"]`` is False.
    """
    action = fix.get("action", "none")
    if action == "none":
        return {"target_file": None, "diff": "", "ok": True,
                "applied": False, "note": "no-op fix"}

    if action not in ACTIONS:
        raise PatchError(f"Unknown action: {action}")

    target_rel = fix.get("target_file")
    if not target_rel:
        raise PatchError("fix is missing target_file")

    # Gate 1: allowlist. Before any filesystem access, so a denied path is not
    # even probed for existence.
    check_allowed(target_rel)

    # Gate 2: traversal.
    try:
        target = resolve_in_repo(target_rel)
    except ConfigError as e:
        raise PathNotAllowed(str(e)) from e

    # Gate 2b: the allowlist again, on the *resolved* path.
    #
    # Gate 1 checks the string the diagnosis asked for. If that path is a
    # symlink pointing elsewhere inside the repo, the bytes land somewhere the
    # write surface never permitted — and invisibly, because the committer
    # stages the declared path, whose own content did not change. Checking only
    # the declared name made ANSIBLE_HEAL_ALLOWED_PATHS bypassable by a symlink
    # the agent itself never created.
    resolved_rel = target.relative_to(repo_root().resolve())
    if resolved_rel != Path(target_rel):
        check_allowed(
            resolved_rel,
            note=f"{target_rel} resolves to {resolved_rel}")

    creating = False
    if not target.exists():
        # Only an action that knows how to start a file from nothing may create
        # one, and only when the diagnosis said so explicitly. Every other
        # action edits an existing file, and a missing target there means the
        # diagnosis is wrong about the repository.
        if action not in CREATABLE or fix.get("create") is not True:
            raise PatchError(f"target_file does not exist: {target_rel}")
        if target.is_symlink():
            raise PathNotAllowed(f"refusing to create {target_rel}: it is a "
                                 f"dangling symlink")
        creating = True

    if creating:
        return _finish(target, target_rel, action, fix, "", dry_run,
                       bom=False, crlf=False)

    # Gate 2c: a hardlink shares an inode with a path the allowlist never saw,
    # and Path.resolve() cannot see it. Truncating in place would write through
    # to every other name for the same bytes.
    try:
        if target.stat().st_nlink > 1:
            raise PathNotAllowed(
                f"refusing to write {target_rel}: it is a hardlink "
                f"({target.stat().st_nlink} names for the same file), so the "
                f"write surface cannot bound where the bytes land.")
    except OSError as e:
        raise PatchError(f"cannot stat {target_rel}: {e}") from e

    try:
        raw = target.read_bytes()
    except OSError as e:
        raise PatchError(f"cannot read {target_rel}: {e}") from e

    # Preserve the file's own encoding conventions. read_text/write_text
    # normalise CRLF to LF and drop a BOM, so a one-line change to a
    # Windows-authored file came out as a whole-file rewrite in the diff, and a
    # BOM took the leading `---` with it.
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    text = raw.decode("utf-8")
    crlf = "\r\n" in text
    original = text.replace("\r\n", "\n") if crlf else text

    # Gate 2d: never write plaintext over encrypted secrets.
    _refuse_if_vault(target, original, target_rel)

    return _finish(target, target_rel, action, fix, original, dry_run,
                   bom=bom, crlf=crlf)


def _finish(target: Path, target_rel: str, action: str, fix: dict,
            original: str, dry_run: bool, *, bom: bool, crlf: bool) -> dict:
    """Compute the patch, validate it, and (unless dry-run) write it."""
    patched = ACTIONS[action](original, fix, target_rel)

    if patched == original:
        raise PatchError(f"{action} on {target_rel} would change nothing")

    # Gate 3: the result must still be parseable. Raising here means nothing
    # was written, so the tree is untouched and the caller can fall back.
    _validate_yaml(target, patched)

    if not dry_run:
        restored = patched.replace("\n", "\r\n") if crlf else patched
        if bom:
            restored = "\ufeff" + restored
        try:
            if not target.parent.exists():
                target.parent.mkdir(parents=True)
            _write_atomically(target, restored)
        except OSError as e:
            # Read-only tree, wrong owner, full disk. A reported failure, not a
            # traceback out of the CLI.
            raise PatchError(f"cannot write {target_rel}: {e}") from e

    return {
        "target_file": target_rel,
        "action": action,
        "ok": True,
        "applied": not dry_run,
        "dry_run": dry_run,
        "diff": unified_diff(original, patched, target_rel),
    }


# ── actions ────────────────────────────────────────────────────────

def _act_edit_file(original: str, fix: dict, target_rel: str) -> str:
    search = fix.get("search")
    replace = fix.get("replace")
    if search is None or replace is None:
        raise PatchError("edit_file fix requires both 'search' and 'replace'")
    if search not in original:
        raise PatchError(
            f"search substring not found in {target_rel}. "
            f"The file may already be patched, or the proposed anchor is wrong."
        )
    return original.replace(search, replace, 1)


def _act_set_yaml_key(original: str, fix: dict, target_rel: str) -> str:
    key = fix.get("key")
    if not key:
        raise PatchError("set_yaml_key fix requires 'key'")
    try:
        return yaml_edit.set_key(original, key, fix.get("value", ""))
    except yaml_edit.YamlEditError as e:
        raise PatchError(f"{target_rel}: {e}") from e


def _act_rename_host(original: str, fix: dict, target_rel: str) -> str:
    old, new = fix.get("old"), fix.get("new")
    if not old or not new:
        raise PatchError("rename_host fix requires 'old' and 'new'")
    try:
        return yaml_edit.rename_host(original, old, new)
    except yaml_edit.YamlEditError as e:
        raise PatchError(f"{target_rel}: {e}") from e


def _act_replace_module(original: str, fix: dict, target_rel: str) -> str:
    old, new = fix.get("old_module"), fix.get("new_module")
    if not old or not new:
        raise PatchError("replace_module fix requires 'old_module' and 'new_module'")
    try:
        return yaml_edit.replace_module(original, old, new, fix.get("new_args"))
    except yaml_edit.YamlEditError as e:
        raise PatchError(f"{target_rel}: {e}") from e


def _act_declare_collection(original: str, fix: dict, target_rel: str) -> str:
    """Add ``fix["collection"]`` to a collections requirements file.

    Existing entries, ``roles:`` and comments survive (round-trip YAML). A
    collection already declared returns the input unchanged, which the caller
    reports as a patch that would change nothing.
    """
    import re
    name = fix.get("collection") or ""
    if not re.match(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$", name):
        raise PatchError(f"declare_collection: invalid collection name {name!r}")
    if not original.strip():
        return f"---\ncollections:\n  - name: {name}\n"
    try:
        data = yaml_edit.load(original)
    except yaml_edit.YamlEditError as e:
        raise PatchError(f"{target_rel}: {e}") from e
    if data is None:
        return f"---\ncollections:\n  - name: {name}\n"
    if not isinstance(data, dict):
        raise PatchError(f"{target_rel} is a legacy roles list, not a "
                         f"collections requirements file")
    entries = data.get("collections")
    if entries is None:
        data["collections"] = [{"name": name}]
    elif not isinstance(entries, list):
        raise PatchError(f"{target_rel}: 'collections' is not a list")
    else:
        for entry in entries:
            existing = entry if isinstance(entry, str) else (
                entry.get("name") if isinstance(entry, dict) else None)
            if isinstance(existing, str) and existing.strip() == name:
                return original
        entries.append({"name": name})
    return yaml_edit.dump(data, explicit_start=yaml_edit.has_document_start(original))


#: Actions allowed to create their target file when it does not exist yet.
CREATABLE = frozenset({"declare_collection"})

ACTIONS = {
    "declare_collection": _act_declare_collection,
    "edit_file": _act_edit_file,
    "set_yaml_key": _act_set_yaml_key,
    "rename_host": _act_rename_host,
    "replace_module": _act_replace_module,
}


def unified_diff(before: str, after: str, path: str = "") -> str:
    """Return a real unified diff, not a hand-rolled approximation."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}" if path else "before",
            tofile=f"b/{path}" if path else "after",
            n=3,
        )
    ).rstrip("\n")


def repo_relative(path: Path) -> str:
    return str(Path(path).resolve().relative_to(repo_root().resolve()))
