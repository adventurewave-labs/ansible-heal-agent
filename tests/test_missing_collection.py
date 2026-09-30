"""Missing-collection failure class: declare, never install; decline otherwise."""

from __future__ import annotations

import subprocess

import pytest

from agent import committer, diagnoser, patcher
from pipeline import git_helper

MOD = "community.docker.docker_container"
REQS = "ansible/collections/requirements.yml"


@pytest.fixture
def not_installed(monkeypatch):
    monkeypatch.setattr(diagnoser, "module_resolves", lambda m: False)
    monkeypatch.setattr(diagnoser, "collection_installed", lambda c: False)


def _diag(module=MOD):
    return diagnoser.fallback_diagnose({"type": "removed_module", "module": module})


def test_proposes_creating_requirements_inside_the_write_surface(scratch_repo, not_installed):
    d = _diag()
    assert d["failure_type"] == "missing_collection"
    fix = d["fix"]
    assert fix == {**fix, "action": "declare_collection", "target_file": REQS,
                   "collection": "community.docker", "create": True}


def test_creates_the_file_and_commits_it(scratch_repo, not_installed):
    d = _diag()
    fix = d["fix"]
    assert not git_helper.check_and_note_clean(fix["target_file"])
    patch = patcher.apply_fix(fix)
    assert patch["applied"]
    text = (scratch_repo / REQS).read_text()
    assert diagnoser.declared_collections(text) == {"community.docker"}
    sha = committer.commit_fix(fix, d)
    assert sha
    subject = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=scratch_repo,
                             capture_output=True, text=True).stdout.strip()
    assert subject == "fix(deps): declare required collection community.docker"


def test_appends_to_an_existing_file_preserving_roles_and_comments(
        scratch_repo, not_installed, monkeypatch):
    path = scratch_repo / "collections" / "requirements.yml"
    path.parent.mkdir()
    path.write_text("---\n# pinned\nroles:\n  - name: geerlingguy.nginx\n"
                    "collections:\n  - name: ansible.posix\n    version: '>=1.5'\n")
    monkeypatch.setenv("ANSIBLE_HEAL_ALLOWED_PATHS", "collections/**,ansible/**")
    d = _diag()
    assert d["fix"]["target_file"] == "collections/requirements.yml"
    assert d["fix"]["create"] is False
    patcher.apply_fix(d["fix"])
    text = path.read_text()
    assert "# pinned" in text and "geerlingguy.nginx" in text
    assert diagnoser.declared_collections(text) == {"ansible.posix", "community.docker"}


def test_existing_file_outside_the_write_surface_is_blocked(scratch_repo, not_installed):
    (scratch_repo / "requirements.yml").write_text("---\ncollections: []\n")
    d = _diag()
    assert d["fix"]["target_file"] == "requirements.yml"
    with pytest.raises(patcher.PathNotAllowed):
        patcher.apply_fix(d["fix"])


def test_already_declared_names_the_install_command(scratch_repo, not_installed):
    (scratch_repo / REQS).parent.mkdir(parents=True)
    (scratch_repo / REQS).write_text("collections:\n  - community.docker\n")
    d = _diag()
    assert d["fix"]["action"] == "none"
    assert f"ansible-galaxy collection install -r {REQS}" in d["_no_fix_reason"]


def test_installed_collection_without_the_module_is_declined(scratch_repo, monkeypatch):
    monkeypatch.setattr(diagnoser, "module_resolves", lambda m: False)
    monkeypatch.setattr(diagnoser, "collection_installed", lambda c: True)
    d = _diag("community.docker.docker_contaner")
    assert d["fix"]["action"] == "none"
    assert "does not provide 'docker_contaner'" in d["_no_fix_reason"]


def test_unknown_install_state_is_declined(scratch_repo, monkeypatch):
    monkeypatch.setattr(diagnoser, "module_resolves", lambda m: False)
    monkeypatch.setattr(diagnoser, "collection_installed", lambda c: None)
    assert "could not ask ansible-galaxy" in _diag()["_no_fix_reason"]


def test_legacy_roles_list_is_declined(scratch_repo, not_installed):
    (scratch_repo / REQS).parent.mkdir(parents=True)
    (scratch_repo / REQS).write_text("- src: geerlingguy.nginx\n")
    assert "legacy roles list" in _diag()["_no_fix_reason"]


def test_builtin_modules_are_not_a_collection_problem(scratch_repo, not_installed):
    d = _diag("ansible.builtin.wat")
    assert "no known replacement" in d["_no_fix_reason"]


def test_invalid_collection_name_is_declined(scratch_repo, not_installed):
    assert "not a valid collection name" in _diag("Bad-Ns.coll.mod")["_no_fix_reason"]


def test_patcher_rejects_an_invalid_name_even_from_an_llm(scratch_repo):
    with pytest.raises(patcher.PatchError, match="invalid collection name"):
        patcher.apply_fix({"action": "declare_collection", "target_file": REQS,
                           "collection": "x; rm -rf /", "create": True})
    assert not (scratch_repo / REQS).exists()


def test_other_actions_still_refuse_a_missing_target(scratch_repo):
    with pytest.raises(patcher.PatchError, match="does not exist"):
        patcher.apply_fix({"action": "set_yaml_key", "create": True,
                           "target_file": "ansible/group_vars/new.yml", "key": "a"})


def test_dry_run_creates_nothing(scratch_repo, not_installed):
    patch = patcher.apply_fix(_diag()["fix"], dry_run=True)
    assert "+  - name: community.docker" in patch["diff"]
    assert not (scratch_repo / REQS).exists()


def test_collection_installed_asks_ansible_galaxy(scratch_repo):
    import shutil
    if not shutil.which("ansible-galaxy"):
        pytest.skip("ansible-galaxy not on PATH")
    assert diagnoser.collection_installed("zz_nonexistent.nothing_here") is False
    assert diagnoser.collection_installed("NOT VALID") is None
