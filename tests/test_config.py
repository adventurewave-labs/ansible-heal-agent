"""Tests for runtime configuration and the write-surface allowlist (PRD NFR-2)."""

from __future__ import annotations

import pytest

from agent import config

# ── repo root resolution ────────────────────────────────────────────────

def test_repo_root_defaults_to_package_checkout(monkeypatch):
    monkeypatch.setattr(config, "_OVERRIDE", None)
    monkeypatch.delenv("ANSIBLE_HEAL_REPO_ROOT", raising=False)
    assert (config.repo_root() / "agent" / "config.py").exists()


def test_env_var_selects_repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_OVERRIDE", None)
    monkeypatch.setenv("ANSIBLE_HEAL_REPO_ROOT", str(tmp_path))
    assert config.repo_root() == tmp_path.resolve()


def test_env_var_pointing_at_a_non_directory_is_an_error(tmp_path, monkeypatch):
    f = tmp_path / "afile"
    f.write_text("x")
    monkeypatch.setattr(config, "_OVERRIDE", None)
    monkeypatch.setenv("ANSIBLE_HEAL_REPO_ROOT", str(f))
    with pytest.raises(config.ConfigError):
        config.repo_root()


def test_override_beats_env_var(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("ANSIBLE_HEAL_REPO_ROOT", str(tmp_path))
    with config.repo_root_override(other):
        assert config.repo_root() == other.resolve()
    assert config.repo_root() == tmp_path.resolve()


def test_override_is_restored_on_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_OVERRIDE", None)
    monkeypatch.delenv("ANSIBLE_HEAL_REPO_ROOT", raising=False)
    with pytest.raises(ValueError):
        with config.repo_root_override(tmp_path):
            raise ValueError("boom")
    assert config._OVERRIDE is None


def test_set_repo_root_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_OVERRIDE", None)
    config.set_repo_root(tmp_path)
    try:
        assert config.repo_root() == tmp_path.resolve()
    finally:
        config._OVERRIDE = None


def test_runs_and_transcripts_dirs_are_created(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_OVERRIDE", tmp_path.resolve())
    assert config.runs_dir().is_dir()
    assert config.transcripts_dir().is_dir()


# ── allowlist ───────────────────────────────────────────────────────

def test_default_allowlist_is_ansible_subtree(monkeypatch):
    monkeypatch.delenv("ANSIBLE_HEAL_ALLOWED_PATHS", raising=False)
    assert config.allowed_paths() == ("ansible/**",)


@pytest.mark.parametrize("rel", [
    "ansible/inventory.yml",
    "ansible/group_vars/all.yml",
    "ansible/playbooks/deeply/nested/site.yml",
])
def test_allowed_paths_inside_default_glob(rel, monkeypatch):
    monkeypatch.delenv("ANSIBLE_HEAL_ALLOWED_PATHS", raising=False)
    assert config.is_path_allowed(rel)


@pytest.mark.parametrize("rel", [
    "agent/core.py",
    "pipeline/runner.py",
    ".github/workflows/ci.yml",
    "ansible_extra/inventory.yml",
    "../outside.yml",
    "ansible/../agent/core.py",
    "/etc/passwd",
])
def test_denied_paths_outside_default_glob(rel, monkeypatch):
    monkeypatch.delenv("ANSIBLE_HEAL_ALLOWED_PATHS", raising=False)
    assert not config.is_path_allowed(rel)


def test_env_var_overrides_allowlist(monkeypatch):
    monkeypatch.setenv("ANSIBLE_HEAL_ALLOWED_PATHS", "infra/**, roles/*.yml")
    assert config.allowed_paths() == ("infra/**", "roles/*.yml")
    assert config.is_path_allowed("infra/prod/main.tf")
    assert config.is_path_allowed("roles/web.yml")
    assert not config.is_path_allowed("ansible/inventory.yml")


def test_empty_allowlist_denies_everything(monkeypatch):
    monkeypatch.setenv("ANSIBLE_HEAL_ALLOWED_PATHS", "")
    assert config.allowed_paths() == ()
    assert not config.is_path_allowed("ansible/inventory.yml")


def test_bare_directory_glob_matches_its_children(monkeypatch):
    monkeypatch.setenv("ANSIBLE_HEAL_ALLOWED_PATHS", "ansible/**")
    assert config.is_path_allowed("ansible/inventory.yml")
    assert config.is_path_allowed("ansible")


def test_empty_path_is_denied(monkeypatch):
    monkeypatch.delenv("ANSIBLE_HEAL_ALLOWED_PATHS", raising=False)
    assert not config.is_path_allowed("")


# ── traversal ───────────────────────────────────────────────────────

def test_resolve_in_repo_rejects_escapes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_OVERRIDE", tmp_path.resolve())
    with pytest.raises(config.ConfigError):
        config.resolve_in_repo("../escape.yml")


def test_resolve_in_repo_returns_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_OVERRIDE", tmp_path.resolve())
    got = config.resolve_in_repo("ansible/inventory.yml")
    assert got == (tmp_path / "ansible" / "inventory.yml").resolve()
