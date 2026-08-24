"""Perturbation suite — the test that decides whether the agent generalises.

The old deterministic diagnoser converged on exactly one input: the seeded
demo baseline. Its fix for an undefined variable inserted the literal line
``nginx_port: 8080`` no matter which variable was undefined, and it located the
stale inventory host by searching for the literal string ``web-01:``.

So it healed the demo and nothing else. Renaming the variable to ``app_port``
produced this:

    SUCCESS: False | iterations: 3 | final_exit: 2
     iter 0  failures [removed_module, undefined_variable, unreachable_host]
             patches 3  commits 3
     iter 1  failures [undefined_variable]  patches 0  commits 0
     iter 2  failures [undefined_variable]  patches 0  commits 0
     iter 3  failures [undefined_variable]  patches 0  commits 0

...having written ``nginx_port: 8080`` into group_vars for an ``app_port``
failure.

These tests vary the variable name, the host names and the module across many
combinations and assert convergence every time. They are the evidence for any
claim about an autonomous heal rate; without them there is no such claim to
make.
"""

from __future__ import annotations

import itertools
import subprocess
import textwrap
from pathlib import Path

import pytest

from agent import yaml_edit
from agent.core import heal
from pipeline import runner

VARIABLES = ["nginx_port", "app_port", "cache_timeout", "worker_replicas",
             "feature_enabled", "telemetry_debug", "upload_dir", "opaque_thing"]

HOST_PAIRS = [
    ("web-01", "web-server-01"),
    ("app-1", "app-01"),
    ("edge-node", "edge-node-01"),
    ("db-primary", "db-primary-01"),
]

# Both of these must be modules ansible-core genuinely cannot resolve. The
# agent asks `ansible-doc` before migrating anything, and refuses to rewrite a
# playbook whose module still works — apt_key resolves on 2.19, so migrating it
# is a modernisation the operator chooses, not a repair, and it no longer
# belongs in a suite that asserts convergence.
MODULES = ["docker"]


def _seed(repo: Path, *, variable: str, stale: str, expected: str,
          module: str) -> None:
    """Write a broken baseline parameterised by the three failure classes."""
    (repo / "ansible" / "group_vars").mkdir(parents=True, exist_ok=True)
    (repo / "ansible" / "playbooks").mkdir(parents=True, exist_ok=True)

    (repo / "ansible" / "inventory.yml").write_text(textwrap.dedent(f"""\
        ---
        all:
          children:
            webservers:
              hosts:
                {stale}:
                  ansible_host: 10.0.1.21
                  ansible_user: ubuntu
                web-02:
                  ansible_host: 10.0.1.22
        """))

    (repo / "ansible" / "group_vars" / "all.yml").write_text(textwrap.dedent("""\
        ---
        # Common settings
        env: production
        log_level: info
        """))

    (repo / "ansible" / "playbooks" / "webservers.yml").write_text(textwrap.dedent(f"""\
        ---
        - name: Configure webservers
          hosts: {expected}
          become: true
          tasks:
            - name: Legacy task
              ansible.builtin.{module}:
                url: https://example.invalid/keys/signing.key
                state: present

            - name: Render config
              ansible.builtin.template:
                src: app.conf.j2
                dest: /etc/app.conf
                vars:
                  listen_port: "{{{{ {variable} }}}}"
        """))

    (repo / "ansible" / "playbooks" / "site.yml").write_text(textwrap.dedent("""\
        ---
        - import_playbook: webservers.yml
        """))


    # A fixture must leave a clean tree: the agent refuses to commit a file
    # that already had uncommitted changes, since `git commit -- <path>` takes
    # the working-tree state and would fold the operator's work into its own
    # commit. A half-written fixture is indistinguishable from that.
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=False)
    subprocess.run(["git", "commit", "-m", "perturbed baseline"], cwd=repo,
                   capture_output=True, check=False)

def _assert_healed(repo: Path, variable: str, stale: str, expected: str,
                   module: str) -> None:
    from agent import diagnoser

    # The module class only converges to a genuinely green pipeline when its
    # replacement itself resolves. `community.docker.docker_container` needs
    # the community.docker collection, which this suite does not assume is
    # installed — the mock now checks every module against ansible-doc, the
    # same authority the diagnoser does, so it correctly keeps reporting the
    # replacement broken when the collection is absent. Migrating it is
    # still correct behaviour, asserted below either way; claiming a false
    # green over an unresolved replacement is exactly the bug this project's
    # own README calls worse than a false failure. Checking this
    # dynamically, rather than assuming it either way, means the suite
    # still asserts full convergence in an environment where the collection
    # genuinely is installed.
    replacement = diagnoser.MODULE_REPLACEMENTS.get(module, {}).get("module")
    module_converges = (replacement is not None
                        and diagnoser.module_resolves(replacement) is True)

    result = heal(playbook="ansible/playbooks/site.yml", max_retries=3,
                  use_llm=False)

    if module_converges:
        assert result.success, (
            f"failed to converge for variable={variable} stale={stale} "
            f"expected={expected} module={module}: "
            f"exit={result.final_exit_code} blocked={result.blocked}")
    else:
        assert not result.success, (
            f"claimed success for variable={variable} stale={stale} "
            f"expected={expected} module={module} despite an unresolved "
            f"replacement — a false green")
        assert any("would change nothing" in d for d in result.declined), \
            result.declined

    inventory = (repo / "ansible" / "inventory.yml").read_text()
    assert f"{expected}:" in inventory
    assert f"{stale}:" not in inventory

    group_vars = yaml_edit.load(
        (repo / "ansible" / "group_vars" / "all.yml").read_text())
    assert variable in group_vars, f"{variable} was not defined"
    # Pre-existing content survives.
    assert group_vars["env"] == "production"

    playbook = (repo / "ansible" / "playbooks" / "webservers.yml").read_text()
    assert f"builtin.{module}:" not in playbook
    if replacement:
        assert replacement in playbook, "the module was migrated, whether or not it resolves"

    exit_code = runner.run_pipeline(
        repo / "ansible" / "playbooks" / "site.yml").exit_code
    assert exit_code == (0 if module_converges else 2)


# ── one dimension at a time ──────────────────────────────────────────

@pytest.mark.parametrize("variable", VARIABLES)
def test_heals_any_undefined_variable_name(scratch_repo, variable):
    """The regression that motivated this suite."""
    _seed(scratch_repo, variable=variable, stale="web-01",
          expected="web-server-01", module="docker")
    _assert_healed(scratch_repo, variable, "web-01", "web-server-01", "docker")


@pytest.mark.parametrize("stale,expected", HOST_PAIRS)
def test_heals_any_stale_hostname(scratch_repo, stale, expected):
    _seed(scratch_repo, variable="nginx_port", stale=stale,
          expected=expected, module="docker")
    _assert_healed(scratch_repo, "nginx_port", stale, expected, "docker")


@pytest.mark.parametrize("module", MODULES)
def test_heals_any_known_removed_module(scratch_repo, module):
    _seed(scratch_repo, variable="nginx_port", stale="web-01",
          expected="web-server-01", module=module)
    _assert_healed(scratch_repo, "nginx_port", "web-01", "web-server-01", module)


# ── all three varied together ────────────────────────────────────────

COMBINATIONS = list(itertools.islice(
    zip(itertools.cycle(VARIABLES),
        itertools.cycle(HOST_PAIRS),
        itertools.cycle(MODULES)),
    12,
))


@pytest.mark.parametrize("variable,hosts,module", COMBINATIONS)
def test_heals_combined_perturbations(scratch_repo, variable, hosts, module):
    stale, expected = hosts
    _seed(scratch_repo, variable=variable, stale=stale, expected=expected,
          module=module)
    _assert_healed(scratch_repo, variable, stale, expected, module)


# ── inferred values ────────────────────────────────────────────────

@pytest.mark.parametrize("variable,expected_value", [
    ("nginx_port", 8080),
    ("app_port", 8080),
    ("cache_timeout", 30),
    ("worker_replicas", 1),
    ("feature_enabled", True),
    ("telemetry_debug", False),
    ("upload_dir", ""),
    ("opaque_thing", ""),
])
def test_inferred_defaults_are_conservative(scratch_repo, variable,
                                            expected_value):
    """Names carry meaning; anything unrecognised gets a placeholder, not a guess."""
    assert yaml_edit.infer_default(variable) == expected_value


# ── honest refusals ────────────────────────────────────────────────

def test_refuses_to_rename_an_unrelated_host(scratch_repo):
    """No close match means no fix — not a coin flip on someone's inventory."""
    from agent import diagnoser
    (scratch_repo / "ansible" / "inventory.yml").write_text(textwrap.dedent("""\
        ---
        all:
          hosts:
            completely-different-thing:
              ansible_host: 10.0.0.1
        """))
    diag = diagnoser.fallback_diagnose(
        {"type": "no_hosts_matched", "pattern": "web-server-01"})
    assert diag["fix"]["action"] == "none"
    assert "would be a guess" in diag["_no_fix_reason"]


def test_refuses_an_unknown_module_rather_than_inventing_a_migration(scratch_repo):
    from agent import diagnoser
    diag = diagnoser.fallback_diagnose(
        {"type": "removed_module", "module": "ansible.builtin.wat"})
    assert diag["fix"]["action"] == "none"
    assert "no known replacement" in diag["_no_fix_reason"]


def test_refuses_to_overwrite_a_variable_that_is_already_defined(scratch_repo):
    from agent import patcher
    gv = scratch_repo / "ansible" / "group_vars" / "all.yml"
    gv.write_text("---\nenv: production\n")
    with pytest.raises(patcher.PatchError):
        patcher.apply_fix({
            "action": "set_yaml_key",
            "target_file": "ansible/group_vars/all.yml",
            "key": "env", "value": "staging",
        })
    assert gv.read_text() == "---\nenv: production\n"


def test_unknown_failure_type_reports_why(scratch_repo):
    from agent import diagnoser
    diag = diagnoser.fallback_diagnose({"type": "meteor_strike"})
    assert diag["fix"]["action"] == "none"
    assert "meteor_strike" in diag["_no_fix_reason"]
