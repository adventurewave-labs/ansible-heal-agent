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
    """Write a fixture file AND commit it.

    Committing matters: the agent refuses to commit a file that already had
    uncommitted changes, because `git commit -- <path>` takes the working-tree
    state and would fold the operator's own work into a commit titled as an
    automated fix. A fixture that leaves files uncommitted is indistinguishable
    from that, and a real target repository is clean.
    """
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=False)
    subprocess.run(["git", "commit", "-m", f"fixture: {rel}"], cwd=repo,
                   capture_output=True, check=False)


def _play(hosts: str) -> str:
    return f"""
        - name: P
          hosts: {hosts}
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """


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


def _commit_fixture(repo: Path) -> None:
    """Commit the fixture's own setup.

    The agent refuses to commit a file that already had uncommitted changes,
    because `git commit -- <path>` takes the working-tree state and would fold
    the operator's work into a commit titled as an automated fix. A fixture
    that writes files without committing them looks exactly like that, so it
    has to leave a clean tree — which is what a real target repository is.
    """
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=False)
    subprocess.run(["git", "commit", "-m", "fixture baseline"], cwd=repo,
                   capture_output=True, check=False)


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
    _commit_fixture(scratch_repo)
    return scratch_repo


# ── secrets ──────────────────────────────────────────────────────

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


# ── the operator's git state ───────────────────────────────────────

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


# ── host patterns that are not stale hostnames ────────────────────────

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


# ── variables the operator has already defined ────────────────────────

def test_a_group_var_covering_every_targeted_host_is_simply_defined(repo):
    """`group_vars/webservers.yml` covers web-01, and the play targets web-01.

    The variable IS defined for every host in scope, so there is nothing to
    report. An earlier version fired a "defined for only some hosts" refusal
    here — on a global name→file map that was never intersected with the play's
    host set, so the message stated a fact nothing had computed.
    """
    _write(repo, "ansible/group_vars/webservers.yml", "nginx_port: 9443\n")
    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    before = (repo / "ansible" / "group_vars" / "all.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert result.success, result.declined
    assert (repo / "ansible" / "group_vars" / "all.yml").read_text() == before


def test_a_variable_defined_for_only_some_targeted_hosts_is_neither_overwritten_nor_ignored(repo):
    """host_vars/web-01.yml defines it; the play targets web-01 AND web-02.

    Two wrong answers are available and the agent has given both. Writing a
    global default overrides the operator's per-host value for everyone else.
    Merging every vars file into one namespace and calling the run green hides
    a play that genuinely fails on web-02. The honest answer is to name the
    file and stop.
    """
    _write(repo, "ansible/host_vars/web-01.yml", "nginx_port: 9443\n")
    _write(repo, "ansible/playbooks/site.yml", _needs_var("all"))
    before = (repo / "ansible" / "group_vars" / "all.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert (repo / "ansible" / "group_vars" / "all.yml").read_text() == before
    assert not result.success
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


# ── the operator's other files ─────────────────────────────────────

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


# ── inputs that used to raise ──────────────────────────────────────

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


# ── the operator's other work in the same repository ────────────────────

def test_a_subdirectory_target_does_not_sweep_the_parent_index(tmp_path, monkeypatch):
    """`git commit` with no pathspec commits the WHOLE index.

    Pointing the agent at a subdirectory of a repository makes it operate on
    that repository's index, so an operator with staged work in progress got it
    committed under a subject claiming to be an Ansible fix.
    """
    from agent import config
    from scenarios import seed

    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    infra = root / "infra"
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=root, check=True)
    for k, v in (("user.email", "t@e.invalid"), ("user.name", "T")):
        subprocess.run(["git", "config", k, v], cwd=root, check=True)
    (root / "app" / "secrets.py").write_text("PASSWORD=original\n")
    for name, body in seed.baseline_files().items():
        path = infra / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")

    (root / "app" / "secrets.py").write_text("PASSWORD=work-in-progress\n")
    _git(root, "add", "app/secrets.py")

    monkeypatch.setattr(config, "_OVERRIDE", infra.resolve())
    heal(max_retries=3, use_llm=False)

    agent_commits = _git(root, "log", "--format=%H", "--grep", "fix(").split()
    for sha in agent_commits:
        touched = _git(root, "show", "--stat", "--format=", sha)
        assert "app/" not in touched, f"{sha[:8]} committed the operator's work"
    assert "app/secrets.py" in _git(root, "diff", "--cached", "--name-only"), \
        "the operator's staged work was taken out of the index"


def test_a_bare_repository_is_recognised(tmp_path):
    """`--is-inside-work-tree` is false in a bare repo, so it read as "not a
    repository" and got a shadow `.git` inside it — masking every ref from
    anything that cloned the path afterwards."""
    from agent import config
    from pipeline import git_helper

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=src, check=True)
    for k, v in (("user.email", "t@e.invalid"), ("user.name", "T")):
        subprocess.run(["git", "config", k, v], cwd=src, check=True)
    (src / "prod.txt").write_text("important\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "p")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)

    with config.repo_root_override(bare):
        git_helper.init_if_needed()

    assert not (bare / ".git").exists(), "created a shadow repository"
    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", str(bare), str(check)], check=True)
    assert (check / "prod.txt").is_file(), "history was masked"


# ── patterns, once more ───────────────────────────────────────────

def test_an_ipv6_literal_with_an_ipv4_tail_is_treated_as_ambiguous(repo):
    """`fd00::21:10.0.0.5` is BOTH a valid IPv6 literal and a union of two
    patterns. Ansible reads it as the union. The IPv6 check added to stop the
    previous round's damage said "one hostname" and a live host was renamed
    away on a repo Ansible runs correctly."""
    _write(repo, "ansible/inventory.yml", """
        all:
          hosts:
            "fd00::21": {}
            "10.0.0.5": {}
    """)
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: "fd00::21:10.0.0.5"
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)
    before = (repo / "ansible" / "inventory.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert (repo / "ansible" / "inventory.yml").read_text() == before
    assert any("ambiguous" in d for d in result.declined), result.declined


def test_a_single_element_list_pattern_still_heals(repo):
    """The raw pattern was stringified from the whole list, so a one-element
    list arrived as "['web-server-01']" and was refused for its brackets."""
    _write(repo, "ansible/inventory.yml", """
        all:
          children:
            g:
              hosts:
                web-01: {}
    """)
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts:
            - web-server-01
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: ok
    """)
    result = heal(max_retries=3, use_llm=False)

    assert result.success, result.declined
    assert "web-server-01" in (repo / "ansible" / "inventory.yml").read_text()


def test_a_lone_exclusion_means_everything_else(repo):
    """`!web-01` is "all except web-01", not "nothing"."""
    _write(repo, "ansible/playbooks/site.yml", _play("'!web-01'"))
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 0, result.failures
    assert result.succeeded_hosts == ["web-02"]


def test_group_indirection_resolves_more_than_one_level(repo):
    """The indirection pass ran once, after the other loop, so a grandparent
    whose child group was defined elsewhere still came out empty."""
    _write(repo, "ansible/inventory.yml", """
        all:
          children:
            webservers:
              hosts:
                web-01: {}
            dc:
              children:
                prod: {}
            prod:
              children:
                webservers: {}
    """)
    _write(repo, "ansible/playbooks/site.yml", _play("dc"))
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 0, result.failures
    assert result.succeeded_hosts == ["web-01"]


def test_an_ordinary_variable_reference_is_detected(repo):
    """The undefined-variable check looked only inside `template: vars:` — a
    key nested in the module arguments, which is the one shape ansible-core
    rejects outright. Every correct way of referencing a variable was invisible,
    so the runner reported exit 0 on playbooks `ansible-playbook` exits 2 on."""
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: web-01
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: "listen {{ nginx_port }}"
    """)
    result = heal(max_retries=3, use_llm=False)

    assert result.success
    assert "nginx_port" in (repo / "ansible" / "group_vars" / "all.yml").read_text()


def test_ansible_core_is_consulted_before_an_inventory_is_edited(repo, monkeypatch):
    """The destructive step asks the authority rather than trusting the sim.

    Six audit rounds found the same shape of bug: the bundled simulator fails
    to match some ordinary Ansible construct, and this diagnoser reads "no
    match" as "the inventory is wrong". Patching the simulator construct by
    construct never converged, so the rename is now gated on ansible-core's own
    answer where ansible-core is available.
    """
    from agent import diagnoser

    monkeypatch.setattr(diagnoser, "ansible_resolves", lambda pattern: True)
    diag = diagnoser.fallback_diagnose(
        {"type": "no_hosts_matched", "pattern": "web-server-01",
         "host": "web-server-01", "raw_pattern": "web-server-01"})

    assert diag["fix"]["action"] == "none"
    assert "ansible-core resolves" in diag["_no_fix_reason"]


# ── the corroboration gate itself ──────────────────────────────────

def test_the_gate_uses_ansibles_own_inventory_resolution(repo):
    """The gate must ask about the inventory ansible-core would actually use.

    It used to hand `ansible` the agent's own guess via `-i`, which overrides
    everything real Ansible would have consulted. On a repo whose ansible.cfg
    lists two inventory files the probe was shown one of them, answered "no
    such host" truthfully, and a live host in the other file was renamed away.
    """
    (repo / "ansible.cfg").write_text(
        "[defaults]\ninventory = ansible/inv_a.yml,ansible/inv_b.yml\n")
    _write(repo, "ansible/inv_a.yml", """
        all:
          hosts:
            web-01: {ansible_host: 10.0.0.1}
    """)
    _write(repo, "ansible/inv_b.yml", """
        all:
          hosts:
            web-02: {ansible_host: 10.0.0.2}
    """)
    (repo / "ansible" / "inventory.yml").unlink()
    _write(repo, "ansible/playbooks/site.yml", _play("web-02"))
    before = (repo / "ansible" / "inv_a.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert (repo / "ansible" / "inv_a.yml").read_text() == before
    assert not result.success
    assert any("ansible-core" in d for d in result.declined), result.declined


def test_the_gate_never_fails_open(monkeypatch):
    """A probe that cannot answer must return None, not False.

    False means "ansible says this pattern matches nothing", which authorises
    the rename. Collapsing "the probe did not run" into that would let any
    environment problem re-open every hole the gate exists to close.
    """
    from agent import diagnoser

    monkeypatch.setattr(diagnoser.shutil, "which", lambda _: None)
    assert diagnoser.ansible_resolves("web-01") is None

    monkeypatch.setattr(diagnoser.shutil, "which", lambda _: "/usr/bin/ansible")
    monkeypatch.setattr(diagnoser.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert diagnoser.ansible_resolves("web-01") is None


def test_a_pattern_that_looks_like_a_flag_is_not_probed(monkeypatch):
    """`ansible -foo --list-hosts` is a usage error, which read as "cannot
    answer" — one more free fail-open."""
    from agent import diagnoser
    monkeypatch.setattr(diagnoser.shutil, "which", lambda _: "/usr/bin/ansible")
    assert diagnoser.ansible_resolves("--become") is None


def test_an_llm_rename_goes_through_the_same_guards(repo, monkeypatch):
    """The guards lived inside fallback_diagnose only, and the LLM is the
    default path. The prompt template asks the model, in as many words, to
    "rename the host in the inventory to the name the playbook targets" — the
    exact destructive act every guard exists to prevent — and nothing between
    the model and the patcher looked at Ansible semantics at all.
    """
    from agent import diagnoser

    _write(repo, "ansible/inventory.yml", """
        all:
          children:
            canary:
              hosts: {}
            webservers:
              hosts:
                canary-01: {}
    """)
    monkeypatch.setattr(diagnoser, "llm_diagnose", lambda failure: {
        "diagnosis": "stale host",
        "failure_type": "unreachable_host",
        "fix": {"action": "rename_host", "target_file": "ansible/inventory.yml",
                "old": "canary-01", "new": "canary", "rationale": "rename"},
    })

    diag = diagnoser.diagnose(
        {"type": "unreachable_host", "pattern": "canary", "host": "canary",
         "raw_pattern": "canary"}, use_llm=True)

    assert diag["fix"]["action"] == "none", diag["fix"]
    assert "is a group" in diag["_no_fix_reason"]


def test_an_llm_rename_onto_an_existing_host_is_refused(repo, monkeypatch):
    from agent import diagnoser

    monkeypatch.setattr(diagnoser, "llm_diagnose", lambda failure: {
        "diagnosis": "stale host",
        "failure_type": "unreachable_host",
        "fix": {"action": "rename_host", "target_file": "ansible/inventory.yml",
                "old": "web-01", "new": "web-02", "rationale": "rename"},
    })

    diag = diagnoser.diagnose(
        {"type": "unreachable_host", "pattern": "web-99", "host": "web-99",
         "raw_pattern": "web-99"}, use_llm=True)

    assert diag["fix"]["action"] == "none", diag["fix"]
    assert "already a host" in diag["_no_fix_reason"]


def test_rename_host_refuses_to_merge_two_hosts(repo):
    """The rebuild is assignment-based and order-preserving, so renaming onto
    an existing key produced a mapping with one entry: two hosts in, one out,
    no error."""
    from agent import yaml_edit
    inventory = (repo / "ansible" / "inventory.yml").read_text()
    with pytest.raises(yaml_edit.YamlEditError) as exc:
        yaml_edit.rename_host(inventory, "web-01", "web-02")
    assert "already a host" in str(exc.value)


def test_the_gate_does_not_run_repository_supplied_inventory_code(repo):
    """`--dry-run` is documented as the mode to point at a repo you have not
    decided to trust. The probe runs ansible with cwd set to that repo, so its
    ansible.cfg could load a third-party inventory plugin — executable code
    from the repository under audit — during a mode that promises to write
    nothing."""
    marker = repo / "EXECUTED"
    (repo / "ansible.cfg").write_text(
        "[defaults]\ninventory_plugins = ./invp\n"
        "[inventory]\nenable_plugins = evilinv, yaml, ini, auto\n")
    plugin = repo / "invp" / "evilinv.py"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text(
        "from ansible.plugins.inventory import BaseInventoryPlugin\n"
        "DOCUMENTATION = 'name: evilinv\\noptions: {}\\n'\n"
        "class InventoryModule(BaseInventoryPlugin):\n"
        "    NAME = 'evilinv'\n"
        "    def verify_file(self, path):\n"
        f"        open({str(marker)!r}, 'w').write('x')\n"
        "        return False\n")
    _write(repo, "ansible/playbooks/site.yml", _play("web-99"))

    from agent import diagnoser
    diagnoser.ansible_resolves("web-99")

    assert not marker.exists(), "ran code supplied by the repository under audit"


# ── variables Ansible supplies for itself ────────────────────────────

def test_a_role_default_is_not_overwritten_with_a_global(repo):
    """group_vars/all outranks a role default, so writing one there does not
    fix a failure — it overrides a working value with a fabricated one. A repo
    deploying /srv/app-1.4.2 quietly started deploying /srv/app-."""
    _write(repo, "ansible/roles/app/defaults/main.yml", 'app_version: "1.4.2"\n')
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: web-01
          gather_facts: false
          tasks:
            - name: use
              ansible.builtin.debug:
                msg: "deploying /srv/app-{{ app_version }}"
    """)
    before = (repo / "ansible" / "group_vars" / "all.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert result.success, result.declined
    assert (repo / "ansible" / "group_vars" / "all.yml").read_text() == before


@pytest.mark.parametrize("name,task_block", [
    ("loop item", '''
        - name: loop
          ansible.builtin.debug:
            msg: "{{ item }}"
          loop: [a, b]
    '''),
    ("set_fact", '''
        - name: fact
          ansible.builtin.set_fact:
            computed_value: 42
        - name: use
          ansible.builtin.debug:
            msg: "{{ computed_value }}"
    '''),
    ("register", '''
        - name: run
          ansible.builtin.command: echo hi
          register: cmd_out
        - name: use
          ansible.builtin.debug:
            msg: "{{ cmd_out }}"
    '''),
])
def test_variables_ansible_binds_itself_are_not_reported_undefined(
        repo, name, task_block):
    """The whole-task scan treats every `{{ x }}` as needing a group_vars entry.
    `item`, a registered result and a set_fact key are the three most common
    idioms in Ansible, and each produced a junk commit writing an empty global.
    """
    body = textwrap.dedent("""
        - name: P
          hosts: web-01
          gather_facts: false
          tasks:
    """).lstrip("\n") + textwrap.dedent(task_block)
    (repo / "ansible" / "playbooks" / "site.yml").write_text(body)
    before = (repo / "ansible" / "group_vars" / "all.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert result.success, result.declined
    assert (repo / "ansible" / "group_vars" / "all.yml").read_text() == before, name


def test_a_top_level_yaml_inventory_resolves(repo):
    """Top-level keys ARE groups when there is no `all:`. This is the standard
    documented layout, and the rule for the *other* shape (a mapping under
    `all:`, which ansible-core skips) was being applied to it — so every
    pattern in such a repo resolved to zero hosts."""
    _write(repo, "ansible/inventory.yml", """
        webservers:
          hosts:
            web-01: {}
            web-02: {}
        dbservers:
          hosts:
            db-01: {}
        prod:
          children:
            webservers: {}
            dbservers: {}
    """)
    _write(repo, "ansible/playbooks/site.yml", _play("prod"))
    result = runner.run_pipeline(repo / "ansible" / "playbooks" / "site.yml")
    assert result.exit_code == 0, result.failures
    assert sorted(result.succeeded_hosts) == ["db-01", "web-01", "web-02"]


# ── what the agent cannot see ──────────────────────────────────────

def test_a_dynamic_inventory_source_stops_every_rename(repo):
    """A repo whose inventory the agent cannot fully see keeps its hosts.

    The host set comes from `ansible-inventory --list` now, not from this
    agent's reading of one file. In dry-run the repo's own inventory plugins
    stay disabled, so a dynamic source makes the answer unavailable — and an
    unavailable answer refuses the rename rather than authorising it. A live
    host in a static file, alongside a dynamic source, used to be renamed away
    on a partial view while the run reported success.
    """
    (repo / "ansible.cfg").write_text(
        "[defaults]\ninventory = ansible/inventory.yml,ansible/inv/dynamic.py\n")
    script = repo / "ansible" / "inv" / "dynamic.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env python3\nprint('{}')\n")
    script.chmod(0o755)
    _write(repo, "ansible/playbooks/site.yml", _play("db-primary-01"))
    before = (repo / "ansible" / "inventory.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert (repo / "ansible" / "inventory.yml").read_text() == before
    assert not result.success
    assert result.declined, "a refusal must be reported, not silence"


def test_an_unparsed_inventory_is_not_a_missing_host(repo, monkeypatch):
    """ansible-core exits 0 and prints `hosts (0)` when it parsed no inventory
    at all — by the number alone, indistinguishable from "that host is absent".
    Only the second is grounds for deleting a host."""
    from agent import diagnoser

    empty = repo / "empty-dir"
    empty.mkdir()
    with __import__("agent.config", fromlist=["config"]).repo_root_override(empty):
        assert diagnoser.ansible_resolves("db-primary-01") is None


# ── variables the agent cannot see ─────────────────────────────────

def test_a_variable_set_in_the_inventory_is_not_undefined(repo):
    """Inventory `vars:` sit BELOW group_vars/all in Ansible's precedence, so
    the "missing" default the agent writes does not fill a hole — it overrides
    a working value. A repo deploying /srv/app-1.4.2 started deploying
    /srv/app-."""
    _write(repo, "ansible/inventory.yml", """
        all:
          hosts:
            web-01: {}
          vars:
            app_version: "1.4.2"
    """)
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: web-01
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: "deploying /srv/app-{{ app_version }}"
    """)
    before = (repo / "ansible" / "group_vars" / "all.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert result.success, result.declined
    assert (repo / "ansible" / "group_vars" / "all.yml").read_text() == before


def test_a_variable_from_vars_files_is_not_undefined(repo):
    _write(repo, "ansible/vars/app.yml", 'app_version: "2.0.0"\n')
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: web-01
          gather_facts: false
          vars_files:
            - ../vars/app.yml
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: "{{ app_version }}"
    """)
    before = (repo / "ansible" / "group_vars" / "all.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert result.success, result.declined
    assert (repo / "ansible" / "group_vars" / "all.yml").read_text() == before


# ── fix shapes the guard has to understand ───────────────────────────

def test_an_llm_edit_file_on_the_inventory_is_refused(repo, monkeypatch):
    """PROMPT_TEMPLATE asks the model for `edit_file` with search/replace, and
    every content check in the guard reads old/new — so for the shape the agent
    itself requests, all of them short-circuited. A host nothing had diagnosed
    was deleted as collateral inside a replace span, and the guard approved it.
    A free-text rewrite of an inventory is not something this agent can
    validate, so it is not allowed to make one.
    """
    from agent import diagnoser

    monkeypatch.setattr(diagnoser, "llm_diagnose", lambda failure: {
        "diagnosis": "stale", "failure_type": "unreachable_host",
        "fix": {"action": "edit_file", "target_file": "ansible/inventory.yml",
                "search": "web-01:", "replace": "web-server-01:", "rationale": "r"},
    })

    diag = diagnoser.diagnose(
        {"type": "unreachable_host", "pattern": "web-server-01",
         "host": "web-server-01", "raw_pattern": "web-server-01"}, use_llm=True)

    assert diag["fix"]["action"] == "none", diag["fix"]
    assert "free text" in diag["_no_fix_reason"]


@pytest.mark.parametrize("spelling", [
    "./ansible/inventory.yml",
    "ansible//inventory.yml",
    "ansible/./inventory.yml",
])
def test_an_inventory_by_another_spelling_is_still_guarded(repo, monkeypatch, spelling):
    """A path the patcher normalises but a string comparison misses used to
    skip every remaining guard."""
    from agent import diagnoser

    monkeypatch.setattr(diagnoser, "llm_diagnose", lambda failure: {
        "diagnosis": "stale", "failure_type": "unreachable_host",
        "fix": {"action": "edit_file", "target_file": spelling,
                "search": "a", "replace": "b", "rationale": "r"},
    })

    diag = diagnoser.diagnose(
        {"type": "unreachable_host", "pattern": "web-server-01",
         "host": "web-server-01", "raw_pattern": "web-server-01"}, use_llm=True)

    assert diag["fix"]["action"] == "none", (spelling, diag["fix"])


# ── the operator's own edits ─────────────────────────────────────

def test_a_second_configured_inventory_is_guarded_too(repo, monkeypatch):
    """An inventory is what ansible.cfg says it is, not what its filename looks
    like. The check consulted only the first source, so an edit_file fix
    targeting the second sailed through unguarded and a replace span deleted
    two live hosts."""
    from agent import diagnoser

    (repo / "ansible.cfg").write_text(
        "[defaults]\ninventory = ansible/prod.yml,ansible/staging.yml\n")
    _write(repo, "ansible/prod.yml", "all:\n  hosts:\n    web-01: {}\n")
    _write(repo, "ansible/staging.yml", "all:\n  hosts:\n    stg-01: {}\n")

    monkeypatch.setattr(diagnoser, "llm_diagnose", lambda failure: {
        "diagnosis": "stale", "failure_type": "unreachable_host",
        "fix": {"action": "edit_file", "target_file": "ansible/staging.yml",
                "search": "stg-01:", "replace": "web-server-01:", "rationale": "r"},
    })

    diag = diagnoser.diagnose(
        {"type": "unreachable_host", "pattern": "web-server-01",
         "host": "web-server-01", "raw_pattern": "web-server-01"}, use_llm=True)

    assert diag["fix"]["action"] == "none", diag["fix"]


def test_a_playbook_named_like_an_inventory_is_not_treated_as_one(repo, monkeypatch):
    """`playbooks/bastion-hosts.yml` was refused because its name contains
    "hosts", and `inventories/prod/group_vars/all.yml` because a path component
    contains "inventories" — both legitimate fixes, both blocked by substring."""
    from agent import diagnoser
    assert not diagnoser._is_inventory_target("ansible/playbooks/bastion-hosts.yml")
    assert not diagnoser._is_inventory_target("inventories/prod/group_vars/all.yml")


def test_the_operators_uncommitted_work_is_never_committed(repo):
    """`git commit -- <path>` commits the WORKING TREE state of that path. The
    pathspec stops other files being swept in; it does nothing about other
    changes to the same file. A staging bump the operator had marked "do not
    ship" went into a commit titled as an automated fix."""
    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    (repo / "ansible" / "group_vars" / "all.yml").write_text(
        "---\nenv: prod\nsecret_debug: true   # WIP, do not ship\n")

    result = heal(max_retries=3, use_llm=False)

    assert not result.success
    assert any("uncommitted changes" in d for d in result.declined), result.declined
    log = _git(repo, "log", "-p", "--format=")
    assert "do not ship" not in log


# ── modules that are not actually broken ────────────────────────────

def test_a_module_that_resolves_is_not_migrated(repo):
    """apt_key resolves on ansible-core 2.19. The simulator declared it removed
    and the agent rewrote a working playbook autonomously — a key *import*
    became a file *download*, dropping `id:` and `state:`."""
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: web-01
          gather_facts: false
          tasks:
            - name: key
              ansible.builtin.apt_key:
                url: https://example.invalid/key.gpg
                id: 1655A0AB68576280
                state: present
    """)
    before = (repo / "ansible" / "playbooks" / "site.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert (repo / "ansible" / "playbooks" / "site.yml").read_text() == before
    assert any("resolves" in d for d in result.declined), result.declined


def test_the_module_probe_reads_the_payload_not_the_exit_code(repo):
    """`ansible-doc` exits 0 for a module it cannot find and says so in the
    output. Reading the exit code alone reported every invented name as
    resolvable — which would have disabled this guard entirely."""
    from agent import diagnoser
    assert diagnoser.module_resolves("apt_key") is True
    assert diagnoser.module_resolves("docker") is False
    assert diagnoser.module_resolves("totally_made_up_xyz") is False


# ── the agent's own preconditions ─────────────────────────────────

def test_the_writing_modes_refuse_without_ansible_core(repo, monkeypatch):
    """Every check that stops the agent damaging a working repository — does
    this host exist, does this module resolve — is answered by ansible-core.
    Without it they all return "unknown", and the modes that write would run
    with no authority behind any of their decisions. `pip install` used to
    produce exactly that: an install whose guards were all inert."""
    import agent.core as core

    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    monkeypatch.setattr(core.shutil, "which",
                        lambda name: None if name.startswith("ansible") else "/bin/true")

    result = heal(max_retries=3, use_llm=False)

    assert not result.success
    assert any("not found on PATH" in d for d in result.declined), result.declined
    assert result.history == [] or not any(r.commits for r in result.history)


def test_a_rename_that_would_orphan_host_vars_is_refused(repo):
    """host_vars/<host>.yml is keyed by name. Renaming the host leaves the file
    behind, silently detaching every variable it defined — connection details
    included — and breaking plays that were working."""
    _write(repo, "ansible/inventory.yml", """
        all:
          hosts:
            web-01: {}
    """)
    _write(repo, "ansible/host_vars/web-01.yml",
           "ansible_connection: local\nnginx_port: 9090\n")
    _write(repo, "ansible/playbooks/site.yml", _play("web-server-01"))
    before = (repo / "ansible" / "inventory.yml").read_text()

    result = heal(max_retries=3, use_llm=False)

    assert (repo / "ansible" / "inventory.yml").read_text() == before
    assert any("orphan" in d for d in result.declined), result.declined


def test_a_file_the_operator_is_editing_is_not_written_to(repo):
    """The refusal used to fire at commit time, by which point the operator's
    work-in-progress had already been overwritten by the agent's edit — and the
    message told them to stash, which would have discarded both together."""
    _write(repo, "ansible/playbooks/site.yml", _needs_var())
    marker = "---\nenv: prod\nsecret_debug: true   # WIP, do not ship\n"
    (repo / "ansible" / "group_vars" / "all.yml").write_text(marker)

    result = heal(max_retries=3, use_llm=False)

    assert (repo / "ansible" / "group_vars" / "all.yml").read_text() == marker, \
        "the agent wrote over work it then refused to commit"
    assert any("in the middle of" in d for d in result.declined), result.declined


def test_a_variable_in_a_group_vars_all_directory_is_seen(repo):
    """`group_vars/all/` may hold any number of files, not just main.yml. Only
    main.yml was read, so a variable in ports.yml was reported undefined
    forever on a repo ansible-playbook exits 0 on."""
    _write(repo, "ansible/group_vars/all/ports.yml", "app_version: 1.4.2\n")
    _write(repo, "ansible/playbooks/site.yml", """
        - name: P
          hosts: web-01
          gather_facts: false
          tasks:
            - name: t
              ansible.builtin.debug:
                msg: "deploying /srv/app-{{ app_version }}"
    """)
    result = heal(max_retries=3, use_llm=False)
    assert result.success, result.declined
