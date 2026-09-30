"""SARIF 2.1.0 output for dry-run findings."""

from __future__ import annotations

import json
import shutil

import pytest
import yaml
from click.testing import CliRunner

from agent import sarif
from agent.core import MODE_DRY_RUN, HealResult, Proposal, heal


def _proposal(**kw):
    base = {
        "failure": {"type": "undefined_variable", "variable": "nginx_port",
                    "playbook": "ansible/playbooks/webservers.yml", "task": "t"},
        "diagnosis": {"diagnosis": "nginx_port is undefined",
                      "failure_type": "undefined_variable",
                      "fix": {"action": "set_yaml_key", "rationale": "default it"}},
        "target_file": "ansible/group_vars/all.yml",
        "diff": "--- a\n+++ b\n+nginx_port: 80\n",
    }
    base.update(kw)
    return Proposal(**base)


def _log(*proposals):
    r = HealResult(success=False, iterations=0, final_exit_code=2, mode=MODE_DRY_RUN)
    r.proposals = list(proposals)
    return sarif.to_sarif(r)


def test_envelope_and_rules(scratch_repo):
    log = _log(_proposal())
    assert log["version"] == "2.1.0" and "sarif-2.1.0" in log["$schema"]
    run = log["runs"][0]
    ids = [r["id"] for r in run["tool"]["driver"]["rules"]]
    assert ids == list(sarif.RULES)
    res = run["results"][0]
    assert res["ruleId"] == "AHA002"
    assert run["tool"]["driver"]["rules"][res["ruleIndex"]]["id"] == "AHA002"


def test_fixable_finding_is_a_warning_with_the_diff(scratch_repo):
    res = _log(_proposal())["runs"][0]["results"][0]
    assert res["level"] == "warning"
    assert "```diff" in res["message"]["markdown"]
    assert res["properties"]["diff"].startswith("--- a")
    rel = res["relatedLocations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert rel == "ansible/group_vars/all.yml"


def test_location_points_at_the_offending_line(scratch_repo):
    text = (scratch_repo / "ansible/playbooks/webservers.yml").read_text()
    expected = next(i for i, ln in enumerate(text.splitlines(), 1) if "nginx_port" in ln)
    loc = _log(_proposal())["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "ansible/playbooks/webservers.yml"
    assert loc["region"]["startLine"] == expected


def test_decline_is_a_note_and_blocked_is_an_error(scratch_repo):
    declined = _proposal(diff="", target_file=None, diagnosis={
        "failure_type": "other", "fix": {"action": "none"},
        "_no_fix_reason": "would be a guess"})
    blocked = _proposal(blocked_reason="outside the allowed write surface")
    results = _log(declined, blocked)["runs"][0]["results"]
    assert results[0]["level"] == "note"
    assert "would be a guess" in results[0]["message"]["text"]
    assert results[1]["level"] == "error"


def test_fingerprints_are_stable_and_distinct(scratch_repo):
    a = _log(_proposal())["runs"][0]["results"][0]["partialFingerprints"]
    b = _log(_proposal())["runs"][0]["results"][0]["partialFingerprints"]
    other = _proposal(failure={"type": "undefined_variable", "variable": "x",
                               "playbook": "ansible/playbooks/webservers.yml"})
    c = _log(other)["runs"][0]["results"][0]["partialFingerprints"]
    assert a == b and a != c


def test_unknown_types_map_to_the_catch_all_rule():
    assert sarif.rule_for({"type": "meteor_strike"}) == "AHA900"
    assert sarif.rule_for({"type": "removed_module"},
                          {"failure_type": "missing_collection"}) == "AHA004"


def test_real_dry_run_produces_one_result_per_seeded_failure(scratch_repo):
    result = heal(mode=MODE_DRY_RUN, use_llm=False)
    log = json.loads(json.dumps(sarif.to_sarif(result)))  # serialisable
    rules = sorted(r["ruleId"] for r in log["runs"][0]["results"])
    assert rules == ["AHA001", "AHA002", "AHA003"]


def test_cli_writes_sarif_and_exits_zero(scratch_repo, tmp_path):
    from agent.cli import cli
    out = tmp_path / "scan.sarif"
    res = CliRunner().invoke(cli, ["run", "--repo", str(scratch_repo), "--dry-run",
                                   "--no-llm", "--no-transcript", "--sarif", str(out)])
    assert res.exit_code == 0, res.output
    assert json.loads(out.read_text())["runs"][0]["results"]

    res = CliRunner().invoke(cli, ["run", "--repo", str(scratch_repo), "--dry-run",
                                   "--no-llm", "--no-transcript", "--sarif", str(out),
                                   "--fail-on-findings"])
    assert res.exit_code == 2


def test_cli_rejects_sarif_outside_dry_run(scratch_repo, tmp_path):
    from agent.cli import cli
    res = CliRunner().invoke(cli, ["run", "--repo", str(scratch_repo),
                                   "--sarif", str(tmp_path / "x.sarif")])
    assert res.exit_code != 0 and "--dry-run" in res.output


def test_composite_action_is_wired_to_the_cli():
    from pathlib import Path
    action = yaml.safe_load((Path(__file__).parent.parent / "action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    scan = next(s for s in action["runs"]["steps"] if s.get("id") == "scan")
    assert "--dry-run" in scan["run"] and "--sarif" in scan["run"]
    # Inputs reach the script through env, never interpolated into bash.
    assert "${{ inputs." not in scan["run"]
    upload = next(s for s in action["runs"]["steps"]
                  if "upload-sarif" in str(s.get("uses", "")))
    assert upload["with"]["category"] == "ansible-heal-agent"


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="no ansible")
def test_sarif_has_no_absolute_paths_in_uris(scratch_repo):
    result = heal(mode=MODE_DRY_RUN, use_llm=False)
    for res in sarif.to_sarif(result)["runs"][0]["results"]:
        for loc in res["locations"] + res.get("relatedLocations", []):
            assert not loc["physicalLocation"]["artifactLocation"]["uri"].startswith("/")
