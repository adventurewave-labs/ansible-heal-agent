"""Repositories the agent must not damage.

Every case here is one where an earlier version of the agent made a confident,
committed change that destroyed something: a secrets file, a production host,
an operator's half-finished merge. They are grouped here rather than in
test_safety.py because what they assert is narrower than a safety invariant —
each one is a specific shape of repository, and the required behaviour is
always the same: refuse, say why, change nothing.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from agent import patcher
from agent.core import heal
from pipeline import runner


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=False).stdout


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))


def _needs_var(hosts: str = "web-01") -> str:
    return f"""
        - name: P
          hosts: {hosts}
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.template:
                vars:
                  port: "{{{{ nginx_port }}}}"
    """


@pytest.fixture
def repo(scratch_repo: Path) -> Path:
    for stale in (scratch_repo / "ansible" / "playbooks").glob("*.yml"):
        stale.unlink()
    _write(scratch_repo, "ansible/inventory.yml", """
        all:
          children:
            webservers:
              hosts:
                web-01: {}
                web-02: {}
    """)
    _write(scratch_repo, "ansible/group_vars/all.yml", "---\nenv: prod\n")
    return scratch_repo


# ── secrets ─────────────────────────────────────────────────────────

VAULT = ("$ANSIBLE_VAULT;1.1;AES256\n"
         "37623433383166326566396633383864613463396264616238\n"
         "6162636465666768696a6b6c6d6e6f707172737475767778\n")


def test_a_vault_encrypted_vars_file_is_never_rewritten(repo):
    """Ciphertext is a valid YAML *scalar*, so a YAML-validity check passes it.

    A string-replace edit then wrote plaintext over the operator's secrets and
    committed the result under a subject about an unrelated fix. There is no
    correct "patch it anyway" here: the agent has no vault password.
    """
    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    (repo / "ansible" / "group_vars" / "all.yml").write_text(VAULT)
    before = (repo / "ansible" / "group_vars" / "all.yml").read_bytes()

    result = heal(max_retries=3, use_llm=False)

    assert (repo / "ansible" / "group_vars" / "all.yml").read_bytes() == before
    assert not result.success
    assert any("vault" in d.lower() for d in result.declined), result.declined


def test_the_patcher_refuses_a_vault_target_directly(repo):
    (repo / "ansible" / "group_vars" / "all.yml").write_text(VAULT)
    with pytest.raises(patcher.VaultRefused):
        patcher.apply_fix({"action": "set_yaml_key",
                           "target_file": "ansible/group_vars/all.yml",
                           "key": "nginx_port", "value": 8080})


def test_a_hardlinked_target_is_refused(repo, tmp_path):
    """resolve() cannot see a hardlink, and write_text truncates in place, so
    the bytes land under every other name for the same inode — including ones
    outside the write surface entirely."""
    outside = tmp_path / "outside.yml"
    outside.write_text("secret: original\n")
    target = repo / "ansible" / "group_vars" / "all.yml"
    target.unlink()
    Path(target).hardlink_to(outside)

    with pytest.raises(patcher.PathNotAllowed) as exc:
        patcher.apply_fix({"action": "set_yaml_key",
                           "target_file": "ansible/group_vars/all.yml",
                           "key": "nginx_port", "value": 8080})

    assert "hardlink" in str(exc.value)
    assert outside.read_text() == "secret: original\n"


# ── the operator's git state ────────────────────────────────────────

def test_a_merge_in_progress_is_never_concluded_by_the_agent(repo):
    """The agent used to `git add` + `git commit` straight through a conflicted
    merge, creating the operator's merge commit for them, titled as an
    automated fix, with their conflict resolution buried inside it."""
    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    (repo / "R.txt").write_text("start\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "feat")
    (repo / "R.txt").write_text("one\n")
    _git(repo, "add", "R.txt")
    _git(repo, "commit", "-m", "f")
    _git(repo, "checkout", "main")
    (repo / "R.txt").write_text("two\n")
    _git(repo, "add", "R.txt")
    _git(repo, "commit", "-m", "m")
    _git(repo, "merge", "feat")
    assert (repo / ".git" / "MERGE_HEAD").exists(), "fixture did not conflict"

    result = heal(max_retries=3, use_llm=False)

    assert (repo / ".git" / "MERGE_HEAD").exists(), "concluded the merge"
    assert not result.success
    assert any("merge is in progress" in d for d in result.declined)


def test_a_detached_head_is_refused(repo):
    """Commits made on a detached HEAD are unreachable the moment the operator
    checks out a branch. The agent used to make them and report success."""
    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "--detach", "HEAD")

    result = heal(max_retries=3, use_llm=False)

    assert not result.success
    assert any("detached HEAD" in d for d in result.declined)


def test_a_commit_git_refuses_is_reported_not_counted(repo):
    """A failing pre-commit hook produced `Success: True`, a phantom empty SHA
    in the transcript, and the agent's edits stranded in the index."""
    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    result = heal(max_retries=3, use_llm=False)

    assert not result.success
    assert any("could not commit" in d for d in result.declined), result.declined
    for record in result.history:
        assert "" not in record.commits, "recorded a phantom empty SHA"


# ── host patterns that are not stale hostnames ──────────────────────

def test_a_group_pattern_never_renames_a_host(repo):
    """An empty group — a tier scaled to zero, a dynamic-inventory placeholder
    — makes real Ansible skip the play and exit 0. Renaming the nearest host to
    match both deleted a live host and created a group/host name collision."""
    _write(repo, "ansible/inventory.yml", """
        all:
          children:
            canary:
              hosts: {}
            webservers:
              hosts:
                canary-01: {}
    """)
    _write(repo, "ansible/playbooks/site.yml", _needs_var("canary"))
    before = (repo / "ansible" / "inventory.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert (repo / "ansible" / "inventory.yml").read_text() == before
    assert any("is a group" in d for d in result.declined), result.declined


def test_localhost_is_never_renamed_into_the_inventory(repo):
    """`hosts: localhost` is one of the most common idioms in Ansible and needs
    no inventory entry. The agent used to repoint a real host at it, so every
    later localhost play in the repo executed against that machine."""
    _write(repo, "ansible/inventory.yml", """
        all:
          children:
            webservers:
              hosts:
                localhost-cache: {ansible_host: 10.0.0.1}
    """)
    _write(repo, "ansible/playbooks/site.yml", _needs_var("localhost"))
    before = (repo / "ansible" / "inventory.yml").read_text()

    heal(max_retries=3, use_llm=False)

    assert (repo / "ansible" / "inventory.yml").read_text() == before


def test_a_whitespace_separated_pattern_is_not_a_hostname(repo):
    """Ansible splits `hosts:` on whitespace as well as commas. Handling only
    the comma left `hosts: web-01 db-01` matching nothing, and the agent
    renamed a host to that literal string."""
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: web-01 web-02
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")

    assert result.exit_code == 0, result.failures
    assert sorted(result.succeeded_hosts) == ["web-01", "web-02"]


def test_a_parent_group_resolves_to_its_childrens_hosts(repo):
    """`children:` was collected one level deep, so a parent group resolved to
    zero hosts and the agent renamed a host to the group's name."""
    _write(repo, "ansible/inventory.yml", """
        all:
          children:
            prod:
              children:
                webservers:
                  hosts:
                    web-01: {}
                    web-02: {}
                dbservers:
                  hosts:
                    db-01: {}
    """)
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: prod
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")

    assert result.exit_code == 0, result.failures
    assert sorted(result.succeeded_hosts) == ["db-01", "web-01", "web-02"]


def test_a_variable_named_hosts_does_not_invent_a_group(repo):
    """Flat-group detection fired on any mapping with a `hosts` key, so a
    `vars:` block containing a variable called `hosts` produced a group named
    `vars` and two hosts that do not exist."""
    _write(repo, "ansible/inventory.yml", """
        all:
          vars:
            hosts:
              primary: web-01
              secondary: web-02
          children:
            webservers:
              hosts:
                web-01: {}
                web-02: {}
    """)
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: all
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")

    assert sorted(result.succeeded_hosts) == ["web-01", "web-02"]


# ── variables the operator has already defined ──────────────────────

def test_a_variable_in_a_group_vars_file_for_one_group_is_seen(repo):
    """Only `group_vars/all*` was read, so a variable set in
    `group_vars/webservers.yml` looked undefined and a fabricated default was
    committed over it."""
    _write(repo, "ansible/group_vars/webservers.yml", "nginx_port: 9443\n")
    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    before = (repo / "ansible" / "group_vars" / "all.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert result.success
    assert (repo / "ansible" / "group_vars" / "all.yml").read_text() == before


def test_a_variable_in_host_vars_is_seen(repo):
    _write(repo, "ansible/host_vars/web-01.yml", "nginx_port: 9443\n")
    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    before = (repo / "ansible" / "group_vars" / "all.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert result.success
    assert (repo / "ansible" / "group_vars" / "all.yml").read_text() == before
