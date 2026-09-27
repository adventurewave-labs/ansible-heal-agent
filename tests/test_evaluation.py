"""The eval harness: scoring rules, aggregation, and a small live run."""

from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner

from agent import evaluation as ev

HEAL = ev.Case("h", "undefined_variable", "heal", {})
DECLINE = ev.Case("d", "no_hosts_matched", "decline", {})


def _score(case, **kw):
    base = {"success": False, "commits": 0, "declined": [], "iterations": 1,
            "final_exit_code": 2, "seconds": 0.1}
    base.update(kw)
    return ev.score(case, **base)


def test_heal_case_scoring():
    assert _score(HEAL, success=True, commits=1).outcome == "healed"
    assert _score(HEAL, commits=0).outcome == "declined"
    r = _score(HEAL, commits=2)
    assert r.outcome == "failed" and not r.correct


def test_green_without_a_commit_is_not_a_heal():
    # A pipeline that was never broken proves nothing about the agent.
    assert _score(HEAL, success=True, commits=0).outcome == "declined"


def test_decline_case_scoring():
    assert _score(DECLINE).correct
    r = _score(DECLINE, commits=1, success=True)
    assert r.outcome == "false_fix" and not r.correct


def test_summary_metrics():
    results = [
        _score(HEAL, success=True, commits=1, iterations=1),
        _score(HEAL, success=True, commits=1, iterations=2),
        _score(HEAL),                       # wrongly declined
        _score(DECLINE),                    # correctly declined
        _score(DECLINE, commits=1),         # false fix
    ]
    s = ev.summarise(results)
    assert s["heal_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert s["false_fix_rate"] == 0.5
    assert s["decline_precision"] == 0.5    # 1 of 2 declines was right
    assert s["mean_iterations"] == 1.5
    assert s["by_class"]["no_hosts_matched"] == {"cases": 2, "correct": 1}


def test_empty_denominators_are_none():
    s = ev.summarise([])
    assert s["heal_rate"] is None and s["false_fix_rate"] is None


def test_corpus_has_both_kinds_and_unique_names():
    corpus = ev.build_corpus()
    names = [c.name for c in corpus]
    assert len(names) == len(set(names))
    assert {c.expect for c in corpus} == {"heal", "decline"}
    assert sum(c.expect == "decline" for c in corpus) >= 4


def test_markdown_report_renders(tmp_path):
    report = {"summary": ev.summarise([_score(HEAL, success=True, commits=1)]),
              "config": {"use_llm": False, "workdir": "x"},
              "results": [vars(_score(HEAL, success=True, commits=1))]}
    j, m = ev.write_report(report, tmp_path)
    assert json.loads(j.read_text())["summary"]["heal_rate"] == 1.0
    assert "| heal rate | 100.0% |" in m.read_text()


needs_ansible = pytest.mark.skipif(
    not all(shutil.which(b) for b in ("ansible", "ansible-inventory", "ansible-doc")),
    reason="apply mode requires ansible-core on PATH")


@needs_ansible
def test_live_eval_heals_and_declines(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_RUNNER", "mock")
    corpus = {c.name: c for c in ev.build_corpus()}
    picked = [corpus["var/app_port"], corpus["host/web-01->web-server-01"],
              corpus["decline/unrelated-host"]]
    report = ev.run_eval(picked, workdir=tmp_path / "w")
    outcomes = {r["name"]: r["outcome"] for r in report["results"]}
    assert outcomes == {  # noqa: SIM300
        "var/app_port": "healed",
        "host/web-01->web-server-01": "healed",
        "decline/unrelated-host": "declined",
    }, report["results"]
    assert report["summary"]["false_fix_rate"] == 0.0


@needs_ansible
def test_cli_eval_gates(tmp_path, monkeypatch):
    from agent.cli import cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PIPELINE_RUNNER", "mock")  # restored after; eval sets it
    res = CliRunner().invoke(cli, ["eval", "--filter", "var/app_port",
                                   "--min-heal-rate", "1.0",
                                   "--max-false-fix-rate", "0"])
    assert res.exit_code == 0, res.output
    assert "heal rate: 100.0%" in res.output
    assert (tmp_path / "eval-report" / "eval-report.json").exists()

    res = CliRunner().invoke(cli, ["eval", "--filter", "no-such-case"])
    assert res.exit_code != 0
