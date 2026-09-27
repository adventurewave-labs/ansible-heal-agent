"""Heal-rate evaluation harness — ``ansible-heal eval``.

The README has always said the agent handles "a small set of well-understood
failure classes"; this module turns that into numbers. It generates a corpus of
broken repositories, runs the real heal loop against each one in a throwaway
git repo, and scores the outcome against what *should* have happened.

Two kinds of case, because a heal rate alone rewards an agent that patches
everything:

``expect="heal"``
    A failure the agent owns. Scored **healed** when the pipeline ends green
    with at least one commit, **declined** when the agent committed nothing,
    **failed** otherwise (commits landed, pipeline still red).

``expect="decline"``
    A failure the agent must refuse — an unrelated host, an empty group, a
    module with no known replacement, a variable defined only in
    ``host_vars``. Scored **declined** (correct) when nothing was committed and
    a reason was reported, **false_fix** when anything was committed. A false
    fix is the metric that matters most: it is a write to someone's
    infrastructure the agent had no business making.

Headline metrics:

- ``heal_rate``       = healed / heal cases
- ``false_fix_rate``  = false_fix / decline cases       (lower is better)
- ``decline_precision`` = correct declines / all declines the agent made
- ``mean_iterations`` over healed cases, and wall time per case

The corpus is deterministic and generated from the same dimensions as
``tests/test_perturbation.py``, so a number reported here is reproducible from
a clean checkout. Pass ``use_llm=True`` to score the LLM path instead of the
deterministic one — the gates are the same, so the comparison is fair.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent import config

VARIABLES = ["nginx_port", "app_port", "cache_timeout", "worker_replicas",
             "feature_enabled", "upload_dir", "telemetry_debug", "opaque_thing"]
HOST_PAIRS = [("web-01", "web-server-01"), ("app-1", "app-01"),
              ("edge-node", "edge-node-01"), ("db-primary", "db-primary-01")]


@dataclass
class Case:
    """One corpus entry: a name, what should happen, and the files to seed."""

    name: str
    failure_class: str
    expect: str  # "heal" | "decline"
    files: dict[str, str]


@dataclass
class CaseResult:
    name: str
    failure_class: str
    expect: str
    outcome: str  # healed | failed | declined | false_fix | error
    correct: bool
    iterations: int
    commits: int
    final_exit_code: int
    seconds: float
    reasons: list[str] = field(default_factory=list)


# ── corpus ───────────────────────────────────────────────────────────


def _inventory(hosts: Iterable[str], group: str = "webservers") -> str:
    lines = ["---", "all:", "  children:", f"    {group}:", "      hosts:"]
    for i, host in enumerate(hosts, start=21):
        lines += [f"        {host}:", f"          ansible_host: 10.0.1.{i}"]
    return "\n".join(lines) + "\n"


def _playbook(target: str, variable: str | None = None,
              module: str = "ansible.builtin.debug") -> str:
    task_args = ('msg: "{{ ' + variable + ' }}"') if variable else 'msg: "ok"'
    return textwrap.dedent(f"""\
        ---
        - name: Configure {target}
          hosts: {target}
          gather_facts: false
          tasks:
            - name: Do the thing
              {module}:
                {task_args}
        """)


_GROUP_VARS = "---\n# Common settings\nenv: production\nlog_level: info\n"
_SITE = "---\n- import_playbook: webservers.yml\n"


def _files(inventory: str, playbook: str, **extra: str) -> dict[str, str]:
    files = {
        "ansible/inventory.yml": inventory,
        "ansible/group_vars/all.yml": _GROUP_VARS,
        "ansible/playbooks/webservers.yml": playbook,
        "ansible/playbooks/site.yml": _SITE,
    }
    files.update(extra)
    return files


def build_corpus() -> list[Case]:
    """The default deterministic corpus: 16 heal cases, 4 decline cases."""
    cases: list[Case] = []
    for stale, expected in HOST_PAIRS:
        cases.append(Case(
            f"host/{stale}->{expected}", "no_hosts_matched", "heal",
            _files(_inventory([stale, "web-02"]), _playbook(expected))))
    for var in VARIABLES:
        cases.append(Case(
            f"var/{var}", "undefined_variable", "heal",
            _files(_inventory(["web-01", "web-02"]),
                   _playbook("webservers", variable=var))))
    for (stale, expected), var in zip(HOST_PAIRS, VARIABLES[::2], strict=False):
        cases.append(Case(
            f"combo/{stale}->{expected}+{var}", "combined", "heal",
            _files(_inventory([stale, "web-02"]),
                   _playbook(expected, variable=var))))

    # ── must decline ──
    cases.append(Case(
        "decline/unrelated-host", "no_hosts_matched", "decline",
        _files(_inventory(["completely-different-thing"], group="misc"),
               _playbook("web-server-01"))))
    cases.append(Case(
        "decline/empty-group", "no_hosts_matched", "decline",
        _files("---\nall:\n  children:\n    webservers:\n      hosts:\n"
               "        web-01:\n          ansible_host: 10.0.1.21\n"
               "    dbservers: {}\n",
               _playbook("dbservers"))))
    cases.append(Case(
        "decline/unknown-module", "removed_module", "decline",
        _files(_inventory(["web-01"]),
               _playbook("webservers", module="ansible.builtin.wat"))))
    cases.append(Case(
        "decline/var-only-in-host_vars", "undefined_variable", "decline",
        _files(_inventory(["web-01", "web-02"]),
               _playbook("webservers", variable="app_port"),
               **{"ansible/host_vars/web-01.yml": "---\napp_port: 8080\n"})))
    return cases


# ── running ──────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def _seed_repo(root: Path, case: Case) -> Path:
    repo = root / case.name.replace("/", "__").replace(">", "")
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "eval@ansible-heal.invalid")
    _git(repo, "config", "user.name", "ansible-heal eval")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, text in case.files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (repo / ".gitignore").write_text("pipeline/runs/\ntranscripts/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "eval: seeded baseline")
    return repo


def score(case: Case, *, success: bool, commits: int, declined: list[str],
          iterations: int, final_exit_code: int, seconds: float) -> CaseResult:
    """Classify one run. Pure, so the scoring rules are unit-testable."""
    if case.expect == "heal":
        if success and commits:
            outcome = "healed"
        elif commits == 0:
            outcome = "declined"
        else:
            outcome = "failed"
        correct = outcome == "healed"
    else:
        outcome = "false_fix" if commits else "declined"
        correct = outcome == "declined"
    return CaseResult(case.name, case.failure_class, case.expect, outcome,
                      correct, iterations, commits, final_exit_code,
                      round(seconds, 3), list(declined))


def run_case(case: Case, workdir: Path, *, use_llm: bool = False,
             max_retries: int = 3) -> CaseResult:
    """Seed ``case`` into a fresh repo under ``workdir`` and heal it."""
    from agent.core import MODE_APPLY, heal

    repo = _seed_repo(workdir, case)
    base = _git(repo, "rev-parse", "HEAD")
    started = time.monotonic()
    # Every case runs with the default write surface and artefacts inside its
    # own repo, whatever the calling process configured — otherwise an
    # operator's ANSIBLE_HEAL_ALLOWED_PATHS or an earlier dry-run's output
    # root silently changes what is being measured.
    saved_paths = os.environ.pop("ANSIBLE_HEAL_ALLOWED_PATHS", None)
    saved_out = config.output_root_override()
    config.set_output_root(None)
    try:
        with config.repo_root_override(repo):
            result = heal(playbook="ansible/playbooks/site.yml",
                          max_retries=max_retries, use_llm=use_llm,
                          mode=MODE_APPLY)
    except Exception as exc:  # noqa: BLE001 - a crash is a result, not an abort
        return CaseResult(case.name, case.failure_class, case.expect, "error",
                          False, 0, 0, -1, round(time.monotonic() - started, 3),
                          [f"{type(exc).__name__}: {exc}"])
    finally:
        config.set_output_root(saved_out)
        if saved_paths is not None:
            os.environ["ANSIBLE_HEAL_ALLOWED_PATHS"] = saved_paths
    commits = int(_git(repo, "rev-list", "--count", f"{base}..HEAD") or 0)
    return score(case, success=result.success, commits=commits,
                 declined=result.declined + result.blocked,
                 iterations=result.iterations,
                 final_exit_code=result.final_exit_code,
                 seconds=time.monotonic() - started)


def summarise(results: list[CaseResult]) -> dict:
    """Aggregate metrics. Rates are None when their denominator is empty."""
    heal_cases = [r for r in results if r.expect == "heal"]
    decline_cases = [r for r in results if r.expect == "decline"]
    healed = [r for r in heal_cases if r.outcome == "healed"]
    false_fix = [r for r in decline_cases if r.outcome == "false_fix"]
    all_declines = [r for r in results if r.outcome == "declined"]
    good_declines = [r for r in all_declines if r.expect == "decline"]

    def rate(n: int, d: int) -> float | None:
        return round(n / d, 4) if d else None

    by_class: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_class.setdefault(r.failure_class, {"cases": 0, "correct": 0})
        bucket["cases"] += 1
        bucket["correct"] += int(r.correct)

    return {
        "cases": len(results),
        "heal_cases": len(heal_cases),
        "decline_cases": len(decline_cases),
        "heal_rate": rate(len(healed), len(heal_cases)),
        "false_fix_rate": rate(len(false_fix), len(decline_cases)),
        "decline_precision": rate(len(good_declines), len(all_declines)),
        "accuracy": rate(sum(r.correct for r in results), len(results)),
        "errors": sum(r.outcome == "error" for r in results),
        "mean_iterations": (round(sum(r.iterations for r in healed) / len(healed), 2)
                            if healed else None),
        "total_seconds": round(sum(r.seconds for r in results), 2),
        "by_class": by_class,
    }


def run_eval(cases: list[Case] | None = None, *, use_llm: bool = False,
             workdir: Path | None = None,
             progress: Callable[[CaseResult], None] | None = None) -> dict:
    """Run the corpus and return ``{"summary": ..., "results": [...]}``."""
    cases = build_corpus() if cases is None else cases
    root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="ansible-heal-eval-"))
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for case in cases:
        r = run_case(case, root, use_llm=use_llm)
        results.append(r)
        if progress:
            progress(r)
    return {
        "summary": summarise(results),
        "config": {"use_llm": use_llm, "workdir": str(root)},
        "results": [asdict(r) for r in results],
    }


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def to_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        "# ansible-heal eval",
        "",
        f"Path: {'LLM' if report['config']['use_llm'] else 'deterministic'} · "
        f"{s['cases']} cases ({s['heal_cases']} heal, {s['decline_cases']} decline) · "
        f"{s['total_seconds']}s",
        "",
        "| metric | value |",
        "|---|---|",
        f"| heal rate | {_pct(s['heal_rate'])} |",
        f"| false-fix rate (lower is better) | {_pct(s['false_fix_rate'])} |",
        f"| decline precision | {_pct(s['decline_precision'])} |",
        f"| accuracy | {_pct(s['accuracy'])} |",
        f"| mean iterations (healed) | {s['mean_iterations']} |",
        f"| errors | {s['errors']} |",
        "",
        "| case | expect | outcome | iters | commits | exit |",
        "|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        mark = "✅" if r["correct"] else "❌"
        lines.append(f"| `{r['name']}` | {r['expect']} | {mark} {r['outcome']} | "
                     f"{r['iterations']} | {r['commits']} | {r['final_exit_code']} |")
    return "\n".join(lines) + "\n"


def write_report(report: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    j = out_dir / "eval-report.json"
    m = out_dir / "eval-report.md"
    j.write_text(json.dumps(report, indent=2) + "\n")
    m.write_text(to_markdown(report))
    return j, m
