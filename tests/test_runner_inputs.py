"""What the runner does with repositories that are not the seeded baseline.

Every fixture elsewhere in this suite describes the same well-formed, single
play, single host repository. These are the shapes that turned up when the
agent was pointed at anything else: valid Ansible the simulator could not
parse, files that were absent or malformed, and patterns whose syntax it never
implemented. Each one of these used to end in a traceback, a crash, or — worst
— a confident "fix" applied to a repository that had been working.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from agent.core import heal
from pipeline import runner


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=False).stdout


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))


@pytest.fixture
def repo(scratch_repo: Path) -> Path:
    """The seeded repo, stripped back to one inventory and no playbooks."""
    for stale in (scratch_repo / "ansible" / "playbooks").glob("*.yml"):
        stale.unlink()
    _write(scratch_repo, "ansible/inventory.yml", """
        all:
          children:
            webservers:
              hosts:
                web-01: {}
                db-01: {}
    """)
    _write(scratch_repo, "ansible/group_vars/all.yml", "---\nenv: prod\n")
    return scratch_repo


def _play(hosts: str) -> str:
    return f"""
        - name: P
          hosts: {hosts}
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """


# ── host patterns the simulator has to get right ────────────────────

def test_a_comma_separated_pattern_is_not_a_missing_host(repo):
    """`hosts: 'web-01,db-01'` is valid Ansible and runs clean.

    The runner matched nothing, so the diagnoser "fixed" the inventory by
    renaming a host to the literal string `web-01,db-01` — breaking a repo that
    had been green, and reporting success while doing it.
    """
    _write(repo, "ansible/playbooks/site.yml", _play("'web-01,db-01'"))
    result = heal(max_retries=3, use_llm=False)

    assert result.success
    assert result.final_exit_code == 0
    assert "web-01,db-01:" not in (repo / "ansible" / "inventory.yml").read_text()
    assert "rename host" not in _git(repo, "log", "--oneline")


def test_a_list_valued_hosts_pattern_does_not_crash(repo):
    """Used to raise TypeError: unhashable type: 'list'."""
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts:
            - web-01
            - db-01
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 0
    assert result.failures == []


def test_a_flat_key_is_not_a_group_because_ansible_says_so(repo):
    """A group mapping placed directly under `all:` is NOT a group.

    An earlier round added "flat group" support believing this shape was common
    and mishandled. Real ansible-core 2.19 disagrees:

        [WARNING]: Skipping unexpected key (webservers) in group (all), only
        "vars", "children" and "hosts" are valid

    and resolves it to zero hosts. Supporting it made the simulator report a
    green run where real Ansible skips the play entirely — a false green is
    worse than a false failure, because nothing downstream questions it.
    """
    _write(repo, "ansible/inventory.yml", """
        all:
          webservers:
            hosts:
              web-01: {}
    """)
    _write(repo, "ansible/playbooks/site.yml", _play("webservers"))
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 2, "matched hosts real Ansible does not see"
    assert result.succeeded_hosts == []


def test_exclusion_intersection_and_regex_patterns_resolve(repo):
    """`:` union, `!` exclusion, `&` intersection and `~regex` are ordinary
    Ansible. Treating them as unevaluable made any repo using them look like a
    failing pipeline."""
    _write(repo, "ansible/playbooks/site.yml", _play("'all:!db-01'"))
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 0, result.failures
    assert result.succeeded_hosts == ["web-01"], result.succeeded_hosts


def test_a_pattern_the_simulator_cannot_parse_is_reported_as_such(repo):
    """The honest answer to a host range is "I cannot evaluate this".

    Reporting it as a missing host was a claim about the repository, and the
    diagnoser acted on that claim.
    """
    _write(repo, "ansible/playbooks/site.yml", _play("'web-0[1:2]'"))
    before = (repo / "ansible" / "inventory.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert not result.success
    types = {f["type"] for f in result.history[0].failures}
    assert types == {"unsupported_pattern"}, types
    assert result.declined, "the operator must be told why nothing happened"
    assert "PIPELINE_RUNNER=real" in result.declined[0]
    assert (repo / "ansible" / "inventory.yml").read_text() == before


# ── inputs that are absent or malformed ─────────────────────────────

def test_absent_group_vars_is_tolerated_not_fatal(repo):
    """group_vars is optional in Ansible. The runner hard-coded all.yml and
    died without it, while the diagnoser offered three possible locations."""
    (repo / "ansible" / "group_vars" / "all.yml").unlink()
    _write(repo, "ansible/playbooks/site.yml", _play("web-01"))

    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 0, result.failures


def test_an_unparseable_playbook_is_reported_not_raised(repo):
    """Used to exit 1 with a yaml.ScannerError traceback."""
    _write(repo, "ansible/playbooks/site.yml", "- name: P\n  hosts: [web-01\n")

    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")

    assert result.exit_code == 2
    assert result.failures, "a repo it cannot read is a finding, not a crash"
    assert result.failures[0]["type"] == "unreadable_input"
    assert "site.yml" in result.failures[0]["message"]


def test_a_missing_inventory_is_reported_not_raised(repo):
    (repo / "ansible" / "inventory.yml").unlink()
    _write(repo, "ansible/playbooks/site.yml", _play("web-01"))

    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 2
    assert result.failures[0]["type"] == "unreadable_input"
    assert "inventory.yml" in result.failures[0]["message"]


def test_a_missing_imported_playbook_is_reported_not_raised(repo):
    _write(repo, "ansible/playbooks/site.yml", "- import_playbook: nope.yml\n")

    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 2
    assert result.failures[0]["type"] == "unreadable_input"


# ── variable precedence ─────────────────────────────────────────────

def test_a_variable_set_on_the_play_is_not_undefined(repo):
    """A play-level `vars:` block satisfies real Ansible.

    Checking group_vars alone made it look undefined, and the agent committed a
    fabricated default into group_vars — silently overriding the operator's own
    value at a lower precedence level.
    """
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: web-01
          vars:
            nginx_port: 9999
          tasks:
            - name: t
              ansible.builtin.template:
                vars:
                  port: "{{ nginx_port }}"
    """)
    before = (repo / "ansible" / "group_vars" / "all.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert result.success
    assert (repo / "ansible" / "group_vars" / "all.yml").read_text() == before


# ── file shapes and encodings ───────────────────────────────────────

def test_a_fifo_is_refused_rather_than_blocking_forever(repo):
    """Opening a FIFO blocks until a writer appears, which in practice is
    never. The run hung until it was killed."""
    import os
    (repo / "ansible" / "inventory.yml").unlink()
    os.mkfifo(repo / "ansible" / "inventory.yml")

    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")

    assert result.exit_code == 2
    assert "not a regular file" in result.failures[0]["message"]


def test_a_list_shaped_inventory_is_reported_not_raised(repo):
    """A list is valid YAML that every caller then indexes as a mapping; it
    escaped as AttributeError from deep inside the run."""
    (repo / "ansible" / "inventory.yml").write_text("- web-01\n- web-02\n")
    _write(repo, "ansible/playbooks/site.yml", _play("web-01"))

    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")

    assert result.exit_code == 2
    assert result.failures[0]["type"] == "unreadable_input"
    assert "not a mapping" in result.failures[0]["message"]


def test_a_vault_encrypted_input_is_named_as_such(repo):
    """"not a mapping" is true of ciphertext and useless to the operator."""
    (repo / "ansible" / "group_vars" / "all.yml").write_text(
        "$ANSIBLE_VAULT;1.1;AES256\n3762343338316632\n")
    _write(repo, "ansible/playbooks/site.yml", _play("web-01"))

    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")

    assert result.exit_code == 2
    assert "vault" in result.failures[0]["message"].lower()


def test_crlf_line_endings_survive_a_patch(repo):
    """read_text/write_text normalise CRLF to LF, so a one-line change to a
    Windows-authored file came out as a whole-file rewrite."""
    from agent import patcher
    target = repo / "ansible" / "group_vars" / "all.yml"
    target.write_bytes(b"---\r\nenv: prod\r\n")

    patcher.apply_fix({"action": "set_yaml_key",
                       "target_file": "ansible/group_vars/all.yml",
                       "key": "nginx_port", "value": 8080})

    raw = target.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b""), "mixed line endings"
    assert b"nginx_port" in raw


def test_a_byte_order_mark_survives_a_patch(repo):
    """The BOM was stripped, and it took the leading `---` with it."""
    from agent import patcher
    target = repo / "ansible" / "group_vars" / "all.yml"
    target.write_bytes(b"\xef\xbb\xbf---\n# comment\nenv: prod\n")

    patcher.apply_fix({"action": "set_yaml_key",
                       "target_file": "ansible/group_vars/all.yml",
                       "key": "nginx_port", "value": 8080})

    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf---"), raw[:20]
    assert b"# comment" in raw


def test_a_partly_failed_write_does_not_truncate_the_original(repo, monkeypatch):
    """write_text truncates first, so ENOSPC left the operator with a file cut
    off mid-key that still parsed as YAML — nothing downstream noticed."""
    from agent import patcher
    target = repo / "ansible" / "group_vars" / "all.yml"
    target.write_text("---\nenv: prod\nkeep: yes\n")
    before = target.read_text()

    real_replace = patcher.os.replace

    def explode(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(patcher.os, "replace", explode)
    with pytest.raises(patcher.PatchError):
        patcher.apply_fix({"action": "set_yaml_key",
                           "target_file": "ansible/group_vars/all.yml",
                           "key": "nginx_port", "value": 8080})
    monkeypatch.setattr(patcher.os, "replace", real_replace)

    assert target.read_text() == before, "the original was damaged"
    leftovers = list((repo / "ansible" / "group_vars").glob(".*tmp"))
    assert leftovers == [], f"left a temp file behind: {leftovers}"
