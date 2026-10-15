"""Integration tests against a real ``ansible-playbook`` process.

These are the tests that decide whether ``PIPELINE_RUNNER=real`` is a feature
or a stub. They shell out to the actual binary — no mock runner, no simulated
log strings — and assert the agent detects, and then heals, failures that a
real ansible-core produces.

Skipped when ansible-core is not installed; CI installs it.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from agent import config
from agent.core import MODE_DRY_RUN, heal
from pipeline import runner

pytestmark = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None,
    reason="ansible-core not installed",
)


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
def real_repo(scratch_repo: Path, monkeypatch) -> Path:
    """A repo whose playbooks real ansible-playbook can actually execute.

    Uses local connections so no SSH is involved, and reproduces the three
    failure classes the agent claims to handle, in the form real ansible-core
    actually reports them.
    """
    monkeypatch.setenv("PIPELINE_RUNNER", "real")

    # web-01 is the entry the host-rename fix is meant to correct, so nothing
    # else may target it: renaming a host another play still needs is refused
    # (and used to make the agent rename it back and forth every iteration).
    _write(scratch_repo, "ansible/inventory.yml", """
        all:
          children:
            webservers:
              hosts:
                web-01:
                  ansible_connection: local
            apps:
              hosts:
                app-01:
                  ansible_connection: local
    """)
    _write(scratch_repo, "ansible/group_vars/all.yml", """
        ---
        env: production
    """)
    # Play 1: host pattern matches nothing. Real ansible WARNS and exits 0.
    _write(scratch_repo, "ansible/playbooks/webservers.yml", """
        - name: Configure webservers
          hosts: web-server-01
          gather_facts: false
          tasks:
            - name: Touch a marker
              ansible.builtin.debug:
                msg: configured
    """)
    # Play 2: undefined variable. Real ansible exits 2.
    _write(scratch_repo, "ansible/playbooks/vars.yml", """
        - name: Render config
          hosts: app-01
          gather_facts: false
          tasks:
            - name: Render listen port
              ansible.builtin.debug:
                msg: "listen {{ nginx_port }}"
    """)
    # Play 3: unresolvable module. Parse-time; real ansible exits 4 and no
    # callback ever fires.
    _write(scratch_repo, "ansible/playbooks/module.yml", """
        - name: Legacy module
          hosts: app-01
          gather_facts: false
          tasks:
            - name: Removed module
              ansible.builtin.docker:
                name: x
    """)
    _write(scratch_repo, "ansible/playbooks/site.yml", """
        - import_playbook: webservers.yml
    """)
    _commit_fixture(scratch_repo)
    return scratch_repo


def _run(repo: Path, playbook: str):
    return runner.run_pipeline(repo / "ansible" / "playbooks" / playbook)


# ── detection ───────────────────────────────────────────────────────

def test_real_runner_detects_a_host_pattern_that_matches_nothing(real_repo):
    """The case the old regexes could never catch: real ansible exits 0."""
    result = _run(real_repo, "webservers.yml")

    assert result.failures, "a silently skipped play must not look healthy"
    assert result.failures[0]["type"] == "no_hosts_matched"
    assert result.exit_code == 2, "reported as a failure despite ansible's exit 0"


def test_no_hosts_failure_is_reported_once_and_names_the_pattern(real_repo):
    """Regression: the callback and the text scan both see this event.

    The callback's record used to carry ``host: None`` and no ``pattern``, so
    it de-duplicated as a *different* failure from the text scan's — every real
    no-hosts run produced two records, and the callback's could not be
    diagnosed at all ("failure did not name the host pattern that failed").
    """
    result = _run(real_repo, "webservers.yml")

    no_hosts = [f for f in result.failures if f["type"] == "no_hosts_matched"]
    assert len(no_hosts) == 1, f"expected one record, got {no_hosts}"
    assert no_hosts[0]["pattern"] == "web-server-01"
    assert no_hosts[0]["host"] == "web-server-01"


def test_no_hosts_failure_from_the_callback_is_diagnosable(real_repo):
    """The record must be actionable on its own, not just present."""
    from agent import diagnoser
    result = _run(real_repo, "webservers.yml")
    no_hosts = [f for f in result.failures if f["type"] == "no_hosts_matched"][0]

    diag = diagnoser.fallback_diagnose(no_hosts)
    assert diag["fix"]["action"] == "rename_host", diag
    assert diag["fix"]["new"] == "web-server-01", diag
    assert diag["fix"]["old"] == "web-01", diag


def test_real_runner_detects_an_undefined_variable_with_its_name(real_repo):
    result = _run(real_repo, "vars.yml")

    undefined = [f for f in result.failures if f["type"] == "undefined_variable"]
    assert undefined, result.failures
    assert undefined[0]["variable"] == "nginx_port"
    assert undefined[0]["host"] == "app-01"


def test_an_undefined_variable_is_reported_once(real_repo):
    """Regression: the callback and the text scan both see this class too.

    De-duplication keyed on (type, host, ...), but the text scan's
    undefined-variable record carries no host at all, so the two never
    collapsed. The operator got two identical proposals in dry-run, and in
    apply mode a spurious "'nginx_port' is already defined" patch failure when
    the second copy was acted on.
    """
    result = _run(real_repo, "vars.yml")

    undefined = [f for f in result.failures if f["type"] == "undefined_variable"]
    assert len(undefined) == 1, f"expected one record, got {undefined}"
    assert undefined[0]["variable"] == "nginx_port"


def test_real_runner_detects_a_parse_time_module_error(real_repo):
    """Exit 4, aborts before any callback fires — text scan is the only source."""
    result = _run(real_repo, "module.yml")

    modules = [f for f in result.failures if f["type"] == "removed_module"]
    assert modules, result.failures
    assert modules[0]["module"] == "ansible.builtin.docker"
    assert modules[0]["module_short"] == "docker"


def test_real_runner_never_reports_failure_without_a_failure(real_repo):
    """The old run_real returned failures=[] for every run, so the agent no-oped."""
    for playbook in ("webservers.yml", "vars.yml", "module.yml"):
        result = _run(real_repo, playbook)
        if result.exit_code != 0:
            assert result.failures, f"{playbook}: nonzero exit with nothing to fix"


def test_real_runner_reports_a_green_run_as_green(real_repo):
    _write(real_repo, "ansible/playbooks/fine.yml", """
        - name: Fine
          hosts: web-01
          gather_facts: false
          tasks:
            - name: Say hello
              ansible.builtin.debug:
                msg: hello
    """)
    result = _run(real_repo, "fine.yml")
    assert result.exit_code == 0
    assert result.failures == []
    assert "web-01" in result.succeeded_hosts


def test_real_runner_writes_a_log_and_a_sidecar(real_repo):
    result = _run(real_repo, "vars.yml")
    log = real_repo / result.log_path
    assert log.exists()
    assert log.with_suffix(".json").exists()


# ── healing ────────────────────────────────────────────────────────

def test_agent_heals_a_real_ansible_run(real_repo):
    """End to end: real ansible-playbook fails, the agent patches, it goes green."""
    result = heal(playbook="ansible/playbooks/site.yml", max_retries=3,
                  use_llm=False)

    assert result.success, f"exit={result.final_exit_code} blocked={result.blocked}"
    inventory = (real_repo / "ansible" / "inventory.yml").read_text()
    assert "web-server-01:" in inventory
    assert "web-01:" not in inventory

    # And prove it against the binary directly, not via our own bookkeeping.
    proc = subprocess.run(
        ["ansible-playbook", "-i", "ansible/inventory.yml",
         "ansible/playbooks/site.yml"],
        cwd=real_repo, capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "ok=1" in proc.stdout


def test_a_real_host_fix_gets_a_real_commit_subject(real_repo):
    """Regression: the real runner's class name fell through to a generic subject.

    _summary_for_type knew `unreachable_host` (the simulator's name) but not
    `no_hosts_matched` (what real Ansible produces), so every real-runner host
    fix committed as "fix(inventory): apply automated fix" while the docs
    advertised the conventional-commit subject.
    """
    heal(playbook="ansible/playbooks/site.yml", max_retries=3, use_llm=False)

    subjects = subprocess.run(["git", "log", "--format=%s"], cwd=real_repo,
                              capture_output=True, text=True).stdout
    assert "fix(inventory): rename host to match playbook expectation" in subjects
    assert "apply automated fix" not in subjects, subjects


def test_agent_heals_an_undefined_variable_against_real_ansible(real_repo):
    """Second class, end to end: real exit 2 → patched group_vars → green."""
    result = heal(playbook="ansible/playbooks/vars.yml", max_retries=3,
                  use_llm=False)

    assert result.success, f"exit={result.final_exit_code} blocked={result.blocked}"
    assert "nginx_port" in (real_repo / "ansible" / "group_vars" / "all.yml").read_text()

    proc = subprocess.run(
        ["ansible-playbook", "-i", "ansible/inventory.yml",
         "ansible/playbooks/vars.yml"],
        cwd=real_repo, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_agent_rewrites_a_removed_module_against_real_ansible(real_repo):
    """Third class, end to end, to the limit of what the class can honestly promise.

    The swap itself is verified against the real binary: before, ansible-core
    cannot resolve ``ansible.builtin.docker`` at all; after, that module is gone
    from the playbook and the name the agent wrote is the one ansible now looks
    for. Without ``community.docker`` installed the re-run fails at resolution
    and names the missing collection. With it installed, the re-run gets past
    resolution and fails at task execution instead (the fixture passes no
    image and no daemon is assumed) — a different failure, but still not the
    one the swap was there to fix, and the swap remains what removed it.
    """
    from tests.helpers import replacement_resolves

    converges = replacement_resolves()
    before = subprocess.run(
        ["ansible-playbook", "-i", "ansible/inventory.yml",
         "ansible/playbooks/module.yml"],
        cwd=real_repo, capture_output=True, text=True, check=False)
    assert before.returncode == 4
    assert "ansible.builtin.docker" in (before.stdout + before.stderr)

    heal(playbook="ansible/playbooks/module.yml", max_retries=2, use_llm=False)

    patched = (real_repo / "ansible" / "playbooks" / "module.yml").read_text()
    assert "ansible.builtin.docker" not in patched, patched
    assert "community.docker.docker_container" in patched, patched
    # Positive evidence the swap is what the agent did, not just what the
    # file happens to contain: it landed as its own conventional commit.
    subjects = subprocess.run(["git", "log", "--format=%s"], cwd=real_repo,
                              capture_output=True, text=True).stdout
    assert "migrate deprecated module" in subjects, subjects

    after = subprocess.run(
        ["ansible-playbook", "-i", "ansible/inventory.yml",
         "ansible/playbooks/module.yml"],
        cwd=real_repo, capture_output=True, text=True, check=False)
    combined = after.stdout + after.stderr
    assert "ansible.builtin.docker" not in combined, combined
    if converges:
        assert after.returncode != 0, combined
        assert "couldn't resolve module/action" not in combined, combined
    else:
        assert "community.docker.docker_container" in combined, combined


def test_a_module_swap_that_cannot_converge_stops_instead_of_looping(real_repo):
    """The stall detector is what keeps an unconvergeable class from spinning.

    Re-proposing the same swap for three iterations would burn the retry budget
    and write three identical commits. Two productive iterations — the swap,
    then declaring the replacement's collection — and then stop. With the
    collection already installed there is no declaration to make: the swap
    genuinely fixes the seeded resolution failure and the run stops on the
    next thing it finds — the fixture's container task is not runnable (no
    image, no daemon), an unclassified failure with no rule — reported
    rather than looped over or self-certified.
    """
    from tests.helpers import replacement_resolves

    converges = replacement_resolves()
    result = heal(playbook="ansible/playbooks/module.yml", max_retries=3,
                  use_llm=False)
    assert not result.success, (
        f"the fixture's container task cannot honestly go green: "
        f"declined={result.declined}")
    # Positive evidence the swap actually happened, in either environment:
    # at least one iteration ran and the migration landed as a commit.
    assert result.iterations >= 1, "never ran the pipeline the swap fixes"
    subjects = subprocess.run(["git", "log", "--format=%s"], cwd=real_repo,
                              capture_output=True, text=True).stdout
    assert "migrate deprecated module" in subjects, subjects
    assert result.iterations <= 2, f"looped {result.iterations} times on a stuck fix"
    if converges:
        assert not any("is declared in" in d for d in result.declined), (
            result.declined)
    else:
        assert any("is declared in" in d for d in result.declined), result.declined


def test_dry_run_against_real_ansible_writes_nothing(real_repo):
    before = (real_repo / "ansible" / "inventory.yml").read_text()
    result = heal(playbook="ansible/playbooks/site.yml", mode=MODE_DRY_RUN,
                  use_llm=False)
    assert result.proposals
    assert (real_repo / "ansible" / "inventory.yml").read_text() == before


def test_probe_recovers_when_ansible_cfg_sits_in_a_world_writable_dir(
        scratch_repo):
    """ansible-core refuses to load an ansible.cfg kept in a world-writable
    directory — the state of every repository checked out under /tmp in a CI
    sandbox. The restricted probe's first attempt relies on the binary reading
    that cfg, so it parses no inventory at all and this used to return
    "unknown", declining the rename on a message that blamed a missing
    ansible-core (issue #20). The retry hands the cfg's own static sources to
    the binary explicitly and recovers the complete host list — without
    executing anything the repository supplies."""
    from agent import diagnoser

    _write(scratch_repo, "ansible.cfg",
           "[defaults]\ninventory = ansible/inventory.yml\n")

    scratch_repo.chmod(0o777)
    try:
        hosts = diagnoser.inventory_hosts_from_ansible(trust_repo=False)
    finally:
        scratch_repo.chmod(0o755)

    assert hosts is not None, (
        "the probe did not recover the host list behind a world-writable "
        "ansible.cfg")
    assert "web-01" in hosts


def test_probe_recovery_covers_every_configured_source(scratch_repo):
    """The retry passes each cfg-configured source as its own ``-i``. A
    comma-joined value would be ambiguous with ansible's inline host-list
    syntax — a filename could surface as a host — so the two-source case
    asserts the exact combined host set: both files' hosts, and nothing
    else, behind the same world-writable cfg the recovery exists for."""
    from agent import diagnoser

    _write(scratch_repo, "ansible.cfg",
           "[defaults]\ninventory = ansible/primary.yml,ansible/secondary.yml\n")
    _write(scratch_repo, "ansible/primary.yml", """
        all:
          hosts:
            web-01:
    """)
    _write(scratch_repo, "ansible/secondary.yml", """
        all:
          hosts:
            extra-09:
    """)

    scratch_repo.chmod(0o777)
    try:
        hosts = diagnoser.inventory_hosts_from_ansible(trust_repo=False)
    finally:
        scratch_repo.chmod(0o755)

    assert hosts == {"web-01", "extra-09"}, (
        "expected exactly both files' hosts and nothing else — a filename "
        f"surfacing as a host means the sources were comma-joined: {hosts}")


def test_probe_recovery_refuses_a_partial_view(scratch_repo):
    """One valid source and one malformed source, both configured, behind a
    world-writable cfg: ansible-core answers with the hosts of the file that
    parses and a warning about the one that does not. That is a partial view
    of the configured inventory, and recovery must refuse it (None) — "the
    hosts I can see" is not "the hosts that exist", and a rename must never
    be decided on the first."""
    from agent import diagnoser

    _write(scratch_repo, "ansible.cfg",
           "[defaults]\ninventory = ansible/primary.yml,ansible/broken.yml\n")
    _write(scratch_repo, "ansible/primary.yml", """
        all:
          hosts:
            web-01:
    """)
    _write(scratch_repo, "ansible/broken.yml", "\tthis: is: not: valid: yaml:\n")

    scratch_repo.chmod(0o777)
    try:
        hosts = diagnoser.inventory_hosts_from_ansible(trust_repo=False)
    finally:
        scratch_repo.chmod(0o755)

    assert hosts is None, (
        "a partially parsed inventory must not be presented as a complete "
        f"host list: {hosts}")


def test_probe_distrusts_answers_from_behind_an_ignored_cfg(
        scratch_repo, monkeypatch):
    """When ansible-core ignores the repository's ansible.cfg — a
    world-writable directory — an answer that still came back, from a
    populated default inventory on that machine, is not this repository's
    host list. The probe must skip it and take the explicit retry's answer.

    The default-inventory fallback cannot be staged for real without
    writing /etc, so the two probe invocations are simulated: the first
    answers with a decoy host behind the cfg-ignored warning, the second
    (the retry carrying the cfg's own sources) with the repository's host.
    """
    import subprocess as sp

    from agent import diagnoser

    _write(scratch_repo, "ansible.cfg",
           "[defaults]\ninventory = ansible/inventory.yml\n")

    calls: list[list[str]] = []

    def fake_run_probe(argv, **kwargs):
        calls.append(argv)
        if "-i" not in argv:
            return sp.CompletedProcess(
                argv, 0,
                '{"_meta": {"hostvars": {"decoy-from-default": {}}}}',
                "[WARNING]: Ansible is being run in a world writable "
                "directory (/tmp/x), ignoring it as an ansible.cfg source.")
        return sp.CompletedProcess(
            argv, 0, '{"_meta": {"hostvars": {"web-01": {}}}}', "")

    monkeypatch.setattr(diagnoser, "_run_probe", fake_run_probe)

    hosts = diagnoser.inventory_hosts_from_ansible(trust_repo=False)
    assert hosts == {"web-01"}, (
        "an answer produced behind an ignored ansible.cfg must not be "
        f"accepted as this repository's host list: {hosts}")
    assert len(calls) == 2 and "-i" in calls[1], (
        "expected the probe to fall through to the explicit retry")


def test_probe_markers_survive_ansible_word_wrapping(
        scratch_repo, monkeypatch):
    """ansible-core word-wraps its warnings to the terminal width, and a long
    repository path splits the no-inventory phrases across lines — "with
    yaml\\nplugin:", "as an inventory\\nsource" — where the probe's regexes
    must still recognise them. CI's long checkout paths hit this for real
    against ansible-core 2.17: a partially parsed inventory was accepted as
    a complete host list."""
    import subprocess as sp

    from agent import diagnoser

    _write(scratch_repo, "ansible.cfg",
           "[defaults]\ninventory = ansible/primary.yml,ansible/broken.yml\n")

    long_dir = "/very/" + "long/" * 30 + "path"

    def fake_run_probe(argv, **kwargs):
        return sp.CompletedProcess(
            argv, 0, '{"_meta": {"hostvars": {"web-01": {}}}}',
            f"[WARNING]:  * Failed to parse {long_dir}/broken.yml with yaml\n"
            "plugin: We were unable to read either as JSON nor YAML")

    monkeypatch.setattr(diagnoser, "_run_probe", fake_run_probe)

    hosts = diagnoser.inventory_hosts_from_ansible(trust_repo=False)
    assert hosts is None, f"a word-wrapped warning went unrecognised: {hosts}"


def test_probe_still_answers_when_the_cfg_names_no_inventory(scratch_repo):
    """A world-writable checkout whose ansible.cfg sets no inventory: the
    first probe attempt carries the conventional inventory file explicitly,
    so its answer never depended on the cfg ansible-core ignored — and must
    not be discarded in the blanket distrust of answers from behind that
    warning. Regression from a review round: it was, and the checkout
    became unanswerable."""
    from agent import diagnoser

    _write(scratch_repo, "ansible.cfg",
           "[defaults]\nretry_files_enabled = False\n")

    scratch_repo.chmod(0o777)
    try:
        hosts = diagnoser.inventory_hosts_from_ansible(trust_repo=False)
    finally:
        scratch_repo.chmod(0o755)

    assert hosts is not None, (
        "an explicit-convention answer was discarded although it never "
        "relied on the ignored ansible.cfg")
    assert "web-01" in hosts


# ── configuration errors ─────────────────────────────────────────────

def test_requesting_the_real_runner_without_ansible_is_an_error(
        scratch_repo, monkeypatch):
    """Silently running the mock instead would be undetectable to an operator."""
    monkeypatch.setenv("PIPELINE_RUNNER", "real")
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    with pytest.raises(runner.RunnerUnavailable):
        runner.run_pipeline(config.repo_root() / "ansible" / "playbooks" / "site.yml")
