# Implementation Plan — Autonomous Ansible Heal Agent

> This Markdown file mirrors the polished DOCX at
> `/home/z/my-project/download/PLAN-Ansible-Heal-Agent.docx`. The DOCX is the
> canonical version; this file is for in-repo reading.

**Owner:** Platform Reliability Engineering
**Status:** Draft — for review
**Date:** 2026-08-20
**Companion to:** `PRD.md` / `PRD-Ansible-Heal-Agent.docx`

---

## 1. Executive Summary

This document specifies the implementation plan for `ansible-heal-agent` v0.1 — an
autonomous agent that heals routine Ansible pipeline failures. The plan is organised
as five build phases (scaffold, mock runner, agent loop, LLM bridge, demo + tests)
plus follow-on sections (component contracts, loop contract, test matrix, production
hardening). Each phase has concrete deliverables, file paths, and acceptance criteria.
Total estimated effort: 8–10 engineer-days for a single engineer familiar with Python
and LLM-based agents.

The MVP ships as a runnable Python repo with a Makefile target (`make demo`) that
resets the repo to a broken baseline, runs the heal loop end-to-end, and writes a
Markdown transcript documenting every agent action. The demo converges in one to two
iterations against a three-failure baseline, exercises both the LLM path (via the
`z-ai` CLI wrapping GLM-4-Plus) and the deterministic fallback path, and is fully
covered by `pytest` unit tests. The codebase is structured so that swapping the mock
Ansible runner for real `ansible-playbook` is a single environment-variable change
(`PIPELINE_RUNNER=real`), and so that adding a new failure class is a single function
addition to `agent/diagnoser.py`.

---

## 2. Architecture & Component Map

The repo is organised into four top-level packages plus an entrypoint script:

```
ansible-heal-agent/
├── ansible/                    # artefacts the agent operates on
│   ├── inventory.yml
│   ├── group_vars/all.yml
│   └── playbooks/
│       ├── site.yml            # imports webservers.yml + db.yml
│       ├── webservers.yml      # intentionally broken in 3 ways
│       └── db.yml              # clean
├── pipeline/                   # mock ansible-playbook + git helper
│   ├── runner.py
│   └── git_helper.py
├── agent/                      # the autonomous agent itself
│   ├── core.py                 # heal loop + Transcript writer
│   ├── llm.py                 # z-ai CLI bridge
│   ├── log_scanner.py
│   ├── diagnoser.py            # LLM + fallback
│   ├── patcher.py              # string-replace + YAML validation
│   ├── committer.py            # conventional-commit
│   ├── pipeline_restarter.py
│   └── cli.py
├── scenarios/
│   └── seed.py                 # canonical broken baseline
├── tests/
│   └── test_agent.py
├── transcripts/                # demo run transcripts (gitignored)
├── demo.py                     # `make demo` entrypoint
├── Makefile
├── pyproject.toml
└── requirements.txt
```

**Dependency direction.** `scenarios/` depends on `pipeline/` (to know the repo
root); `pipeline/` depends on nothing internal; `agent/` depends on `pipeline/`
(for git and runner types); `tests/` depends on everything. There are no circular
imports. The CLI entrypoint (`agent/cli.py` and `demo.py`) wires the pieces
together and writes the transcript.

**External dependencies.** Two are critical: `PyYAML` for parsing and writing
the YAML artefacts, and the `z-ai` CLI (provided by the host environment) for
the LLM bridge. `rich` and `click` are convenience dependencies for CLI
ergonomics. The codebase deliberately avoids any heavyweight agent framework
(no LangChain, no AutoGen) — the loop is small enough to be a single function,
and avoiding the framework makes the agent's behaviour fully transparent and
auditable in the transcript.

---

## 3. Phased Build Plan

### 3.1 Phase 0 — Repo scaffold (0.5 day)

Create the directory skeleton, `pyproject.toml`, `requirements.txt`, `.gitignore`,
`README.md`, and `Makefile`. Initialise a git repo.

**Acceptance:** `pip install -r requirements.txt && python3 -c "import agent, pipeline,
scenarios"` succeeds with no errors.

### 3.2 Phase 1 — Mock Ansible runner (1.5 days)

Implement `pipeline/runner.py`: parse `ansible/playbooks/site.yml`, expand
`import_playbook` directives, walk each play's tasks. Implement two-phase
execution: **Phase A** parse-time validation (detect removed modules and undefined
variables), **Phase B** runtime execution (resolve host pattern against inventory,
emit UNREACHABLE for missing hosts). Write structured log to
`pipeline/runs/run-<ts>.log`. Implement `run_pipeline()` public entrypoint that
dispatches to mock or real based on `PIPELINE_RUNNER` env var. Implement
`pipeline/git_helper.py`: `init_if_needed`, `add`, `commit`, `log`.

**Acceptance:** `python3 -m pipeline.runner ansible/playbooks/site.yml` exits 2
and produces a log containing all three failure signatures.

### 3.3 Phase 2 — Agent loop + fallback diagnoser (2 days)

Implement:
- `agent/log_scanner.py` — regex + sidecar JSON extraction
- `agent/diagnoser.py` — `fallback_diagnose()` for the three classes + `diagnose()` wrapper
- `agent/patcher.py` — string-replace + YAML validation
- `agent/committer.py` — conventional-commit message builder
- `agent/pipeline_restarter.py` — thin wrapper around `pipeline.runner.run_pipeline`
- `agent/core.py` — the heal loop with `IterationRecord` + `HealResult` dataclasses
  + `Transcript` writer

**Acceptance:** `python3 -c "from agent.core import heal; heal(use_llm=False)"`
converges in ≤3 iterations against the broken baseline, leaving the repo in a
green state.

### 3.4 Phase 3 — LLM bridge (1 day)

Implement `agent/llm.py`:
- `is_available()` — checks `z-ai` on PATH
- `chat()` — shells out to `z-ai chat --prompt <prompt> --output <tmpfile>` and
  parses the JSON response
- `chat_json()` — strips markdown fences and extracts the JSON object

Wire `diagnoser.diagnose()` to call `llm.chat_json()` with the structured prompt
template, falling back to `fallback_diagnose()` on any error (LLM unavailable,
malformed JSON, validation failure).

**Acceptance:** `make demo` shows LLM-originated diagnoses for at least two of
the three failures, with the YAML-validation layer catching and falling back on
any malformed LLM patches.

### 3.5 Phase 4 — Demo + transcripts + tests (1 day)

Write `demo.py` (entrypoint that resets baseline, runs heal, writes transcript).
Write `scenarios/seed.py` with the canonical broken baseline strings
(`INVENTORY_BROKEN`, `GROUP_VARS_BROKEN`, `WEBSERVERS_BROKEN`, `DB_PLAYBOOK`,
`SITE_PLAYBOOK`) and `reset_to_baseline()` that writes them to disk and commits.
Write `tests/test_agent.py` covering: baseline fails with 3 failures, log scanner
extracts all types, fallback diagnoser for each class, patcher applies + is
idempotent, full heal loop converges with fallback only.

**Acceptance:** `make demo` produces a transcript showing the agent healing all
three failures; `make test` is green.

---

## 4. Component Contracts

### 4.1 Pipeline watcher / runner

`pipeline.runner.run_pipeline(playbook_path, run_id) -> RunResult(exit_code,
log_path, failures, succeeded_hosts)`. The mock runner is deterministic: same
inputs produce same outputs. Failures are returned as structured dicts with
keys: `type`, `host` (optional), `pattern`/`module`/`variable` (depending on
type), `message`, `playbook`, `play`, `task`. The real runner
(`PIPELINE_RUNNER=real`) wraps `ansible-playbook` and returns `exit_code` +
`log_path`, with `failures` left empty (the log scanner then extracts them via
regex).

### 4.2 Log scanner

`agent.log_scanner.extract_failures(log_path) -> list[dict]`. Prefers the
structured sidecar JSON written by the mock runner; falls back to regex
extraction over the raw log lines for three patterns: `UNREACHABLE`,
`couldn't resolve module action`, `undefined variable`.
`summarise_failures(failures) -> str` returns a one-line human summary for
transcripts.

### 4.3 Diagnoser

`agent.diagnoser.diagnose(failure, use_llm=True) -> dict`. Returns a diagnosis
with keys: `diagnosis` (one-sentence root cause), `failure_type`, `fix {action,
target_file, search, replace, rationale}`. On any LLM error or shape validation
failure, falls back to `fallback_diagnose(failure)`, which handles the three
known classes deterministically. The `_fallback_reason` key is added when
fallback was used, so the transcript is honest about the source of each patch.

### 4.4 Patcher

`agent.patcher.apply_fix(fix) -> dict`. Applies the fix's `search`→`replace`
substitution to the target file (single occurrence). Before writing, validates
the patched content is still parseable YAML for `.yml`/`.yaml` files; on
validation failure, raises `PatchError` without writing. Returns a dict with
`target_file`, `occurrences_replaced`, `before_lines`, `after_lines`, `ok=True`,
and a mini-diff string for the transcript. Raises `PatchError` if the action is
unknown, the target file doesn't exist, the search string isn't found, or the
patched YAML is invalid.

### 4.5 Committer

`agent.committer.commit_fix(fix, diagnosis) -> sha`. Stages the fix's target
file via `git_helper.add()`, builds a conventional-commit message
(`type(scope): summary`, then body with root cause and rationale), and creates
the commit via `git_helper.commit()`. Returns the commit SHA, or empty string
if nothing was staged. Scopes: `inventory`, `vars`, `playbook`, `repo`.

### 4.6 Pipeline restarter

`agent.pipeline_restarter.restart(playbook, run_id) -> RunResult`. Thin wrapper
around `pipeline.runner.run_pipeline()` that resolves the playbook path
relative to the repo root. The restarter is the only component allowed to
invoke the runner from inside the heal loop.

---

## 5. Agent Loop Contract

The heal loop in `agent/core.py` implements:

```python
def heal(playbook, max_retries=3, use_llm=True, transcript=None):
    git_helper.init_if_needed()
    for i in range(max_retries + 1):
        run = pipeline_restarter.restart(playbook=playbook, run_id=f"iter{i}")
        if transcript: transcript.iteration_header(i, run)
        rec = IterationRecord(i, run.log_path, run.exit_code, run.failures)
        if run.exit_code == 0:
            mark success; break
        if i == max_retries:
            mark give_up; break
        for failure in run.failures:
            diag = diagnoser.diagnose(failure, use_llm)
            try:
                patch = patcher.apply_fix(diag.fix)
            except PatchError:
                if use_llm and diag is not fallback:
                    fb_diag = fallback_diagnose(failure)
                    patch = patcher.apply_fix(fb_diag.fix)
                else: continue
            sha = committer.commit_fix(diag.fix, diag)
            rec.commits.append(sha)
        history.append(rec)
    return HealResult(...)
```

**Three invariants** hold at every iteration boundary:

1. **Git working tree is always clean between iterations.** Every patch is
   committed before the next iteration begins, so a re-run starts from a known
   state.
2. **The iteration cap (default 3) is hard.** The loop never runs more than
   `max_retries+1` iterations, even if every iteration produces new failures.
3. **The transcript is append-only.** Every event (failure found, diagnosis,
   patch applied, patch failed, committed, iteration green, give-up) is
   recorded with timestamps and full context, so a human reviewing the
   transcript can reconstruct the agent's exact reasoning chain.

---

## 6. Failure Scenarios — Test Matrix

| Scenario | Seed file | Detection signature | Patch target | Commit message |
|----------|-----------|---------------------|--------------|-----------------|
| `unreachable_host` | `inventory.yml` (web-01) vs `webservers.yml` (web-server-01) | `fatal: [web-server-01]: UNREACHABLE!` | `inventory.yml`: rename `web-01` → `web-server-01` | `fix(inventory): rename host to match playbook expectation` |
| `removed_module` | `webservers.yml` (`ansible.builtin.apt_key` task) | `ERROR! couldn't resolve module action 'apt_key'` | `webservers.yml`: replace `apt_key` with `get_url` + `command` | `fix(playbook): migrate deprecated module to modern equivalent` |
| `undefined_variable` | `webservers.yml` (`{{ nginx_port }}`) + `group_vars/all.yml` (missing) | `FAILED! => msg=The task includes an option with an undefined variable 'nginx_port'` | `group_vars/all.yml`: add `nginx_port: 80` | `fix(vars): add missing variable to group_vars` |

Each scenario is also exercised by a dedicated unit test in `tests/test_agent.py`
that asserts the fallback diagnoser produces the expected fix shape and that the
patcher successfully applies it. The end-to-end demo additionally verifies that
the LLM produces a semantically equivalent patch (the LLM may choose
`nginx_port: 80` instead of `8080`, or a slightly different module replacement,
but the pipeline must still go green after the patch).

---

## 7. Testing Strategy

**Unit-test layer** (`tests/test_agent.py`) covers each component in isolation:
the runner produces the expected failure set against the broken baseline, the
log scanner extracts all three failure types, the fallback diagnoser produces
correctly-shaped fixes for each class, the patcher applies patches and is
idempotent (a second apply raises `PatchError` because the search string is no
longer present), and the full heal loop converges using only the fallback path.
Unit tests use an autouse fixture that calls `scenarios.seed.reset_to_baseline()`
before each test, ensuring isolation.

**Integration-test layer** is the demo script itself (`demo.py`). It exercises
the full loop with the LLM bridge enabled (when available) and writes a
transcript that a human can review. The transcript is the integration-test
artefact: a human reading it should be able to verify that the agent made
sensible decisions at each step. The demo is also runnable in a fallback-only
mode (`--no-llm`) for environments where the `z-ai` CLI is not available.

**Smoke-test layer** is `make test`, which runs pytest. CI contract: every
pull request must pass `make test`. The demo is not run in CI (it requires
the `z-ai` CLI and produces a timestamped transcript that would clutter the
repo), but a CI smoke job runs the demo with `--no-llm` on every push to main
and asserts that the heal loop converges, providing a continuous guarantee
that the fallback path remains functional.

---

## 8. Production Hardening

Moving from the MVP to a production-ready agent requires five hardening passes:

1. **Real Ansible.** Swap the mock runner for real `ansible-playbook`
   (`PIPELINE_RUNNER=real`). Requires the agent to handle real Ansible's noisier
   log output, which is less structured than the mock; the log scanner's regex
   path must be extended to handle additional failure signatures (syntax
   errors, SSH connection refused, privilege escalation failures). Real Ansible
   also writes JSON events when running with `-vv`, which the scanner can
   consume as a structured sidecar.

2. **PR-mode (`--require-human-approval`).** Instead of committing directly to
   the working branch, the agent opens a pull request via `gh` CLI and waits
   for human approval before re-running the pipeline. This is the appropriate
   default for shared production playbooks; the MVP's direct-commit mode is
   reserved for the agent's own demo repo and for personal IaC repos where the
   user has explicitly opted in.

3. **Scoped credentials.** The CI container in which the agent runs must have
   a deploy key scoped to a single IaC repo, with read-only SSH keys, and no
   access to production secrets. The agent never needs production secrets — it
   only edits YAML and runs git — so the principle of least privilege is
   straightforward to enforce.

4. **OpenTelemetry instrumentation.** Each component (scan, diagnose, patch,
   commit, restart) emits a span with structured attributes (`failure_type`,
   `target_file`, `llm_used`, `iterations`). Spans are exported to the team's
   observability stack and aggregated into the KPI dashboard described in the
   PRD.

5. **Multi-region support.** The MVP is single-region by design; production
   deployments may need to run the agent in multiple regions simultaneously,
   each healing pipelines in its own region. This requires the agent to be
   stateless (it is, today) and the transcript archive to be partitioned by
   region. A central audit dashboard aggregates transcripts across regions.

---

## 9. Open Questions & Risks

1. **LLM YAML indentation is unreliable.** The LLM may produce a multi-task
   replacement where the second task is mis-indented, breaking the playbook's
   syntax. The MVP handles this by validating the patched YAML before writing
   and falling back to the deterministic diagnoser, but the production agent
   should consider a more sophisticated patch-application strategy (e.g.
   parsing the LLM's replacement as a YAML fragment and re-serialising it with
   correct indentation).

2. **The fallback diagnoser is essential but limited in coverage.** It handles
   only the three shipped failure classes; any novel failure falls through to
   `failure_type=other` and is surfaced to the human on-call. The production
   agent should be evaluated against a broader labelled dataset of real
   Ansible failures to determine whether the fallback's coverage is sufficient
   or whether additional classes need to be added.

3. **The iteration cap (default 3) may be too aggressive** for failure
   cascades where the first heal surfaces a previously-masked second failure.
   The MVP's iteration cap is hard-coded; the production agent should make it
   configurable per-pipeline, with a sensible default informed by fleet-wide
   data.

4. **Transcripts are the killer feature for human trust.** The MVP's transcripts
   are detailed and timestamped; the production agent should consider adding a
   web UI that renders transcripts as an interactive timeline, allowing reviewers
   to expand each step and see the LLM's full prompt and response. Without a
   usable transcript UI, the agent's decisions are opaque and human trust will
   not scale.

---

## 10. Appendix: Demo Runbook

To run the demo end-to-end, clone the repo and execute:

```bash
$ cd ansible-heal-agent
$ pip install -r requirements.txt   # PyYAML, click, rich, pytest
$ make demo
```

**Expected output:** the demo prints a four-step progress banner (reset baseline,
configure LLM bridge, run heal loop, save transcript), followed by a success line
and the transcript path. The heal loop should converge in one or two iterations,
leaving the git log with three new conventional commits — one per healed failure.

Open the transcript at `transcripts/demo-<timestamp>.md` to inspect the agent's
reasoning chain. The transcript shows: each pipeline run's exit code and failure
count; each failure's structured JSON; each diagnosis (LLM or fallback); each
patch's diff; each commit's SHA. The transcript is the primary artefact for human
review and is the basis for the agent's audit trail in production.

To run the demo without the LLM bridge (for environments where the `z-ai` CLI is
not available), use `python3 demo.py --no-llm` (or set `use_llm=False` in
`agent.core.heal()`). The demo will still converge using the deterministic
fallback diagnoser, though the diagnoses will be less varied.
