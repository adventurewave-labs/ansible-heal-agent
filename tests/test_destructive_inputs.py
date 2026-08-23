"""Repositories the agent must not damage.

Every case here is one where an earlier version of the agent made a confident,
committed change that destroyed something: a secrets file, a production host,
an operator's half-finished merge. They are grouped here rather than in
test_safety.py because what they assert is narrower than a safety invariant —
each one is a specific shape of repository, and the required behaviour is
always the same: refuse, say why, change nothing.
"""

from __future__ import annotations

import re
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

def test_a_variable_defined_for_only_some_hosts_is_neither_overwritten_nor_ignored(repo):
    """`group_vars/webservers.yml` defines it; `group_vars/all.yml` does not.

    Two wrong answers were available and the agent has given both. Writing a
    global default overrides the operator's per-group value for every other
    host. Merging every vars file into one namespace and calling the run green
    hides a play that genuinely fails on a host the variable was not defined
    for. The honest answer is to say which file defines it and stop.
    """
    _write(repo, "ansible/group_vars/webservers.yml", "nginx_port: 9443\n")
    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    before = (repo / "ansible" / "group_vars" / "all.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert (repo / "ansible" / "group_vars" / "all.yml").read_text() == before
    assert not result.success
    assert any("webservers.yml" in d for d in result.declined), result.declined


def test_a_variable_defined_in_host_vars_is_treated_the_same_way(repo):
    _write(repo, "ansible/host_vars/web-01.yml", "nginx_port: 9443\n")
    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    before = (repo / "ansible" / "group_vars" / "all.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert (repo / "ansible" / "group_vars" / "all.yml").read_text() == before
    assert any("web-01.yml" in d for d in result.declined), result.declined


# ── patterns the runner must not mangle before the guard sees them ──

def test_an_ipv6_host_is_not_split_on_its_colons(repo):
    """`fd00::21` is one host. Ansible is IPv6-aware; the runner was not.

    It split the pattern into the tokens `fd00` and `21` *before* the
    diagnoser's "is this one hostname?" guard ran, so the guard was applied to
    a fragment that looked perfectly ordinary — and a real address was renamed
    to `fd00`. The guard is only as good as the layer it runs at.
    """
    _write(repo, "ansible/inventory.yml", """
        all:
          children:
            v6:
              hosts:
                "fd00::22": {ansible_user: ubuntu}
    """)
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: 'fd00::21'
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)

    result = heal(max_retries=3, use_llm=False)

    inventory = (repo / "ansible" / "inventory.yml").read_text()
    assert "fd00::21" in inventory, "did not heal to the address the play wants"
    assert "fd00::22" not in inventory
    # The fragment the old split produced. `fd00:` as a bare key would be a
    # different host entirely, and the play would still match nothing.
    assert not re.search(r"^\s+fd00:\s*$", inventory, re.M), "renamed to a fragment"
    assert result.success


def test_an_ipv6_pattern_that_matches_is_not_a_failure(repo):
    _write(repo, "ansible/inventory.yml", """
        all:
          children:
            v6:
              hosts:
                "fd00::21": {}
    """)
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: 'fd00::21'
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 0, result.failures


def test_a_question_mark_glob_matches_rather_than_renaming(repo):
    """`?` is an Ansible glob. It was in neither the matcher nor the guard's
    metacharacter set, so a working repo had its host renamed to `othe?`."""
    _write(repo, "ansible/inventory.yml", """
        all:
          children:
            g:
              hosts:
                other: {ansible_host: 10.0.1.9}
    """)
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: 'othe?'
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 0, result.failures
    assert result.succeeded_hosts == ["other"]


def test_a_glob_matches_group_names_too(repo):
    """Real Ansible resolves `prod*` against groups as well as hosts."""
    _write(repo, "ansible/inventory.yml", """
        all:
          children:
            production:
              hosts:
                alpha: {}
                beta: {}
    """)
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: 'prod*'
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 0, result.failures
    assert sorted(result.succeeded_hosts) == ["alpha", "beta"]


# ── the operator's other files ──────────────────────────────────────

def test_a_worktree_is_recognised_as_a_repository(repo, tmp_path):
    """In a worktree — and in a submodule — `.git` is a *file*, not a directory.

    Reading that as "not a repository" made the agent `git init` on top of a
    real one and, before the same commit fixed it, `git add .` the operator's
    untracked work into a commit authored by the agent.
    """
    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    tree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(tree), "-b", "wt")
    (tree / "secret-wip.txt").write_text("unrelated\n")

    from agent import config
    before = _git(tree, "rev-list", "--count", "HEAD").strip()
    config.set_repo_root(tree)
    try:
        heal(max_retries=3, use_llm=False)
    finally:
        config.set_repo_root(repo)

    assert (tree / ".git").is_file(), "fixture is not a worktree"
    assert "secret-wip.txt" in _git(tree, "status", "--porcelain")
    after = _git(tree, "rev-list", "--count", "HEAD").strip()
    assert int(after) >= int(before), "history was rewritten"


def test_initialising_a_directory_does_not_commit_what_was_already_there(
        tmp_path, monkeypatch):
    """`git init` + `git add .` swept whatever happened to be in the directory
    — a stray .env, a work-in-progress file — into a commit authored by the
    agent. Initialising someone's directory is presumptuous enough."""
    from agent import config
    from scenarios import seed

    target = tmp_path / "plain"
    (target / "private").mkdir(parents=True)
    (target / "private" / ".env").write_text("AWS_SECRET=hunter2\n")
    for name, body in seed.baseline_files().items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    monkeypatch.setattr(config, "_OVERRIDE", target.resolve())

    heal(max_retries=3, use_llm=False)

    committed = subprocess.run(
        ["git", "log", "--all", "--name-only", "--format="],
        cwd=target, capture_output=True, text=True).stdout
    assert ".env" not in committed, "committed a file it was never asked to touch"


# ── inputs that used to raise ───────────────────────────────────────

@pytest.mark.parametrize("name,playbook,expect", [
    ("import cycle", "- import_playbook: site.yml\n", "cycle"),
    ("tasks not a list", "- name: P\n  hosts: web-01\n  tasks: notalist\n",
     "not a list"),
    ("task not a mapping", "- name: P\n  hosts: web-01\n  tasks: [notadict]\n",
     "not a mapping"),
    ("import escape", "- import_playbook: ../../../etc/hosts\n",
     "outside the repository"),
])
def test_malformed_playbooks_are_reported_not_raised(repo, name, playbook, expect):
    (repo / "ansible" / "playbooks" / "site.yml").write_text(playbook)
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 2, name
    assert result.failures[0]["type"] == "unreadable_input", name
    assert expect in result.failures[0]["message"], result.failures[0]["message"]


def test_a_non_string_inventory_host_key_does_not_crash(repo):
    """YAML turns bare `1234:` into an int, and difflib raised TypeError."""
    _write(repo, "ansible/inventory.yml", """
        all:
          children:
            g:
              hosts:
                1234: {}
    """)
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: web-01
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)
    result = heal(max_retries=3, use_llm=False)
    assert not result.success
    assert result.declined


def test_a_refusal_is_reported_once_not_once_per_iteration(repo):
    _write(repo, "ansible/inventory.yml", """
        all:
          children:
            empties:
              hosts: {}
            g:
              hosts:
                empty-01: {}
    """)
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: empties
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)
    result = heal(max_retries=3, use_llm=False)
    assert len(result.declined) == len(set(result.declined))


def test_the_inventory_named_by_ansible_cfg_is_the_one_used(repo):
    """Both runners hardcoded ansible/inventory.yml, so the normal way to lay
    a repo out — pointing ansible.cfg somewhere else — read as "no inventory"."""
    (repo / "ansible.cfg").write_text("[defaults]\ninventory = inventories/prod.yml\n")
    _write(repo, "inventories/prod.yml", """
        all:
          children:
            g:
              hosts:
                web-01: {}
    """)
    (repo / "ansible" / "inventory.yml").unlink()
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: web-01
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 0, result.failures


def test_the_words_vault_in_a_comment_do_not_block_a_patch(repo):
    """The tag check scanned the whole file for the literal string `!vault`."""
    (repo / "ansible" / "group_vars" / "all.yml").write_text(
        "---\n# this comment mentions !vault for no reason\nenv: prod\n")
    _write(repo, "ansible/playbooks/site.yml", _needs_var())

    result = heal(max_retries=3, use_llm=False)

    assert result.success, result.declined
