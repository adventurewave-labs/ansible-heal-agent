# PRD — Autonomous Ansible Heal Agent

> This file is the canonical PRD. Section 6 carries a per-requirement status
> column; sections describing v0.1 as simulated are annotated where the
> implementation has since moved past them.

**Owner:** Marcus Patman
**Status:** v0.1 implemented; see §6 for per-requirement status
**Date:** 2026-08-20 (status column 2026-08-21)
**Version:** v0.1

---

## 1. Executive Summary

Ansible-Heal-Agent is an autonomous software agent that monitors an Ansible-driven
deployment pipeline, diagnoses routine failures from the pipeline log stream, patches
the offending playbooks / inventory / variable files in-tree, commits the fix to git
using conventional-commit messages, and re-runs the pipeline until it goes green.

The MVP targets three well-known, high-frequency failure classes:

1. **stale hostnames** after infrastructure renames,
2. **deprecated-or-removed** Ansible modules,
3. **undefined variables** referenced from templates.

The agent is LLM-first: a model performs the natural-language diagnosis and
proposes a structured JSON patch. The provider is not fixed — Anthropic,
OpenRouter and the `z-ai` CLI are auto-detected in that order. A deterministic
rule-based fallback guarantees the agent still converges when no provider is
configured, or when the model is rate-limited or produces malformed output. Every action the agent takes — prompts,
responses, patches, commits, pipeline re-runs — is captured in a timestamped Markdown
transcript stored alongside the repo, giving human reviewers full visibility into the
agent's reasoning chain.

**Expected impact:** mean-time-to-resolution for the targeted failure classes drops
from a typical 15–40 minute human-driven window to under 60 seconds; on-call SREs
are paged only for novel failures the agent explicitly declines to handle. The MVP
ships with a simulator (no real SSH, no real apt) so the design space can be
validated safely; `PIPELINE_RUNNER=real` drives a real `ansible-playbook`
process and is covered by integration tests.

---

## 2. Problem Statement

Ansible is the dominant config-management tool for mid-to-large infrastructure
fleets, and Ansible-driven CI/CD pipelines fail constantly. Industry postmortems
and internal incident reviews repeatedly surface the same handful of routine
failure classes: a host was renamed during a migration and the inventory file was
not updated in lock-step; a community module was deprecated and removed in an
ansible-core upgrade, but the playbook still references it; a template grew a new
variable reference that was never added to `group_vars`. None of these failures
requires human creativity to resolve. Each follows a small, well-understood
diagnostic-then-patch pattern that an LLM with codebase context can execute
reliably.

Despite their routine nature, these failures impose outsized cost. An on-call SRE
interrupts whatever they were doing, opens the failing pipeline log, identifies
the failure class, opens the relevant playbook or inventory file, types a
one-line fix, commits, and re-runs. The end-to-end cycle is 15–40 minutes on a
good day, considerably longer at 03:00. Mean-time-to-resolution (MTTR) for the
broader incident is gated by this human-in-the-loop cycle even when the
underlying fix is trivially mechanical. Over a quarter, hundreds of engineer-hours
are consumed by fixes that are, in hindsight, deterministic.

The opportunity is to delegate this class of work to an autonomous agent that
never tires, never miscopies a hostname, and never forgets the modern replacement
for a removed module. The agent should not attempt to fix novel failures — those
still require human judgement — but for the bounded set of routine failures it
should resolve them faster, more reliably, and with a complete audit trail that
humans can review the next morning.

---

## 3. Target Audience & Personas

**Primary — On-call SRE rotation.** Paged when a deployment pipeline fails outside
business hours; their goal is to restore green status with the smallest, safest
possible change. They will use the agent's transcript as the first thing they
read when they pick up the pager, and they will override or revert the agent's
commits when they disagree. The agent is not a replacement for the on-call
engineer; it is a force multiplier that handles the boring 80% so the human can
focus on the interesting 20%.

**Secondary — Infrastructure platform team.** Owns the Ansible playbooks as a
product. They benefit indirectly: every successful autonomous heal is a data
point about which failure classes are common enough to warrant prevention
(e.g. deprecation linting in CI), and the agent's transcript archive becomes
a corpus for improving playbooks and runbooks.

**Tertiary — Engineering leadership.** Cares about MTTR, on-call load, and the
speed at which the platform can absorb change (renames, upgrades, refactors).
The agent makes the cost of these changes visible — a rename that previously
caused three days of pipeline breakage now heals itself, which changes the
calculus of when to undertake such changes.

---

## 4. Goals & Non-Goals

### 4.1 Goals

1. Automatically detect pipeline failures within 5 seconds of the pipeline's
   non-zero exit by tailing the Ansible log stream.
2. Classify each failure into one of the three supported failure classes
   (`unreachable_host`, `removed_module`, `undefined_variable`) with at least
   95% accuracy as measured against a labelled validation set.
3. Propose and apply a structured patch via the LLM, with deterministic fallback
   when the LLM is unavailable or returns malformed output.
4. Commit the patch with a conventional-commit message scoped to the touched
   file (e.g. `fix(inventory): rename host to match playbook expectation`).
5. Re-run the pipeline and repeat the loop until green or until three iterations
   have been exhausted, whichever comes first.

### 4.2 Non-Goals

The MVP explicitly does not attempt to handle novel failures — failures outside
the three supported classes are surfaced to the human on-call with the agent's
best-effort diagnosis attached as a comment, but no patch is applied. The agent
does not push to remote, open pull requests, or trigger downstream deploys; in
the MVP, all changes are local commits in a single-branch working tree. The
agent does not perform multi-region failover, capacity planning, or any form of
stateful recovery (e.g. restoring a corrupted database). It is not a CI/CD
replacement; it consumes the existing pipeline's exit code and log stream as
inputs and respects the existing pipeline's restart semantics. Finally, the MVP
does not handle playbook-syntax errors that prevent Ansible from parsing the
playbook at all — those are caught by `ansible-lint` in pre-commit and are out
of scope for the runtime agent.

---

## 5. User Stories & Agent Architecture

**Core user story (SRE persona):**

> As an on-call SRE, when an Ansible pipeline fails with a stale hostname,
> removed module, or undefined variable, I want the agent to automatically
> diagnose and fix the failure, commit the fix, and re-run the pipeline, all
> within 60 seconds, so that I am only paged for failures the agent cannot
> handle.

**Secondary user story (platform engineer):**

> As a platform engineer reviewing the previous night's activity, I want a
> Markdown transcript of every agent action — prompts, LLM responses, patches
> applied, commits made, pipeline re-runs — so I can audit and improve the
> playbooks.

**Architecture.** The agent is a single-process loop with five components:

| Component              | Responsibility                                                                |
|------------------------|-------------------------------------------------------------------------------|
| Pipeline watcher       | Tails the mock runner's log file (or real Ansible log stream).               |
| Log scanner            | Extracts structured failure records from raw log lines + sidecar JSON.       |
| Diagnoser (LLM + FB)   | Calls the configured LLM provider; falls back to rule-based on any error.    |
| Patcher                | Applies a bounded edit (string replace or structural YAML); validates before writing. |
| Committer              | Stages the touched file, creates a conventional commit.                     |
| Pipeline restarter     | Re-runs the pipeline; loop continues until green or iteration cap.           |

**Loop contract.** At most three iterations. Iteration 0 always runs the pipeline
and discovers the initial failure set. Iterations 1–2 diagnose, patch, commit, and
re-run. If iteration 3 still fails, the agent declares defeat and writes a
"give up" record to the transcript, surfacing the remaining failures to the human
on-call. The iteration cap exists to prevent runaway commit loops when the LLM
repeatedly proposes patches that don't actually fix the underlying problem.

---

## 6. Functional Requirements

> **Status column added 2026-08-21.** Every row now records whether the
> requirement is actually implemented, and where. Several MUSTs sat in this
> table for months with no implementing code while the README advertised them
> as configuration switches. A PRD whose MUSTs are honestly marked
> NOT IMPLEMENTED is useful; one whose MUSTs silently do not exist is
> decoration.

| ID    | Priority | Requirement | Status |
|-------|----------|-------------|--------|
| FR-1  | MUST     | Agent detects pipeline non-zero exit within 5 seconds. | **MET** — `pipeline/runner.py`; sub-second in both runners |
| FR-2  | MUST     | Agent extracts structured failure records (type, host, message, playbook, task). | **MET** — `pipeline/callback_plugins/heal_json.py` for real runs, `agent/log_scanner.py` for parse-time errors |
| FR-3  | MUST     | Agent calls an LLM for diagnosis; falls back to rule-based on any error. | **MET, RESTATED** — was "GLM-4-Plus via z-ai CLI", which pinned the requirement to one vendor's container. Now Anthropic / OpenRouter / z-ai, auto-detected (`agent/llm.py`) |
| FR-4  | MUST     | Patcher applies a bounded edit to an allowed target file. | **MET, WIDENED** — string replace plus three structural YAML actions; a string-only patcher could not handle a variable name it had not seen |
| FR-5  | MUST     | Patcher validates the patched file is still valid YAML before writing. | **MET** — `agent/patcher.py`, asserted byte-unchanged on rejection |
| FR-6  | MUST     | Committer creates a conventional-commit scoped to the touched file's directory. | **MET** — `agent/committer.py` |
| FR-7  | MUST     | Agent re-runs the pipeline after each commit, up to max 3 iterations. | **MET** — plus stall detection: a round that patches nothing stops rather than re-running an identical failure set |
| FR-8  | MUST     | Agent writes a Markdown transcript with timestamps, prompts, responses, diffs, commits. | **MET** — example committed at `docs/example-transcript.md` |
| FR-9  | SHOULD   | Agent supports `PIPELINE_RUNNER=real` to call real `ansible-playbook`. | **MET** — was a stub returning an empty failure list; now covered by `tests/test_real_ansible.py` against the real binary |
| FR-10 | SHOULD   | Agent supports PR-mode (`--require-human-approval`) that opens a PR instead of committing directly. | **MET** — `agent/core.py::_heal_pr` |
| FR-11 | COULD    | Agent handles `ssh_conn_refused` failures with retry/wait_for logic. | NOT IMPLEMENTED — detected and reported as `unreachable_host`; no remediation rule |
| FR-12 | COULD    | Agent posts a summary to Slack on completion (success or give-up). | NOT IMPLEMENTED |
| NFR-1 | MUST     | End-to-end MTTR for a single healed failure ≤ 60 seconds on commodity hardware. | **MET** — ~0.3s for three failures against the simulator |
| NFR-2 | MUST     | Agent never writes outside `ANSIBLE_HEAL_ALLOWED_PATHS` globs (default: `ansible/**`). | **MET** — the env var previously appeared nowhere in the codebase; enforced in `agent/patcher.py`, `tests/test_safety.py` |
| NFR-3 | MUST     | Agent never force-pushes, never rewrites history, never touches main without `--require-human-approval` in PR-mode. | **MET** — the flag previously did not exist; asserted by base-branch-SHA-unchanged tests |
| NFR-4 | MUST     | Agent is idempotent: re-running `heal()` on an already-green pipeline is a no-op. | **MET** — asserted by commit count, not by inspection |
| NFR-5 | SHOULD   | Agent emits OpenTelemetry spans for each component. | **NOT IMPLEMENTED** — roadmap |
| NFR-6 | WON'T    | Agent does not perform multi-region failover or capacity planning. | n/a |

Each MUST requirement is covered by at least one test and exercised end-to-end
by `demo.py` in CI. The suite is 216 tests across `tests/test_agent.py`,
`test_safety.py`, `test_perturbation.py`, `test_real_ansible.py`,
`test_config.py`, `test_cli.py` and `test_llm.py`.

---

## 7. Failure Scenarios Handled by MVP

### 7.1 Scenario A — Stale Hostname (`no_hosts_matched`)

The playbook `ansible/playbooks/webservers.yml` targets host `web-server-01`, but
`ansible/inventory.yml` lists the host as `web-01` (the pre-migration name).

Measured against ansible-core 2.19: real Ansible prints `skipping: no hosts
matched` on stdout, `[WARNING]: Could not match supplied host pattern, ignoring:
web-server-01` on stderr, and exits **0**. A play that silently configured zero
hosts looks like a healthy deployment to anything watching the exit code, which
is the entire reason `pipeline/callback_plugins/heal_json.py` exists — the
callback sees the event, and `run_real` reports it as a failure with exit 2.

The mock runner emits an `UNREACHABLE` line with a nonzero exit instead. That is
a divergence from real Ansible, documented in `pipeline/runner.py`'s docstring;
the simulator's class name is `unreachable_host` and the real one is
`no_hosts_matched`. Both map to the same fix.

The LLM diagnoses the root cause as a stale inventory entry and proposes renaming
`web-01` to `web-server-01` in `inventory.yml`. The deterministic fallback proposes
the identical fix.

**Expected commit:** `fix(inventory): rename host to match playbook expectation`

### 7.2 Scenario B — Removed Module (`removed_module`)

The playbook uses `ansible.builtin.apt_key` to add the nginx signing key.

Measured against ansible-core 2.19: `apt_key` still **resolves** — it is
deprecated, not removed, and the module file ships with no `deprecated:` block.
Only the underlying `apt-key` command is deprecated. So the fix the agent
proposes for it is a modernisation, not a repair of a broken play, and the mock
runner's parse-time rejection of it is simulation rather than reproduction.

The module class *is* verified against the real binary, using
`ansible.builtin.docker`, which genuinely does not resolve: real Ansible emits
`[ERROR]: couldn't resolve module/action 'ansible.builtin.docker'. This often
indicates a misspelling, missing collection, or incorrect module path.` and
exits **4**,
before any callback fires — which is why the text scan exists alongside the
callback. See `tests/test_real_ansible.py`.

The LLM proposes replacing the task with `ansible.builtin.get_url` +
`ansible.builtin.command` to fetch the key and add it to the keyring. The
deterministic fallback proposes a slightly different replacement (just `get_url`
to a keyring path). Both are valid; the LLM's version is preferred because it
includes the `apt-key add` step.

**Expected commit:** `fix(playbook): migrate deprecated module to modern equivalent`

### 7.3 Scenario C — Undefined Variable (`undefined_variable`)

The playbook's template task references `{{ nginx_port }}` in its `vars` block,
but `nginx_port` is not defined in `ansible/group_vars/all.yml`.

Measured against ansible-core 2.19: real Ansible emits `fatal: [web-01]:
FAILED! => {"msg": "Task failed: … Error while resolving value for 'msg':
'nginx_port' is undefined"}` and exits 2. The string `The task includes an
option with an undefined variable` is the **mock runner's** phrasing, not
Ansible's; `agent/log_scanner.py` carries both, and the callback plugin matches
the real one on the event rather than the text.

The mock detects this statically, by collecting template-var references and
checking them against `group_vars` plus any `vars:` the play or task sets
itself.

The LLM proposes adding `nginx_port: 80` to `group_vars/all.yml`. The deterministic
fallback proposes `nginx_port: 8080`. Both are valid; the agent accepts whichever
the LLM proposes first.

**Expected commit:** `fix(vars): add missing variable to group_vars`

---

## 8. Security & Safety Model

The agent's safety model is built on five layers, each of which must hold for the
agent to take an action.

1. **LLM contract.** The LLM is prompted to return a structured JSON object with
   an exact `target_file`, `search`, and `replace` triple; it is explicitly told
   which files it is allowed to touch.
2. **Patcher pre-write validation.** The patcher loads the patched content as
   YAML and refuses to write if the result is unparseable, preventing the LLM
   from accidentally breaking the playbook's syntax.
3. **Allowed-paths glob.** The patcher refuses to write to any file not matching
   `ANSIBLE_HEAL_ALLOWED_PATHS` (default: `ansible/**`).
4. **Git history.** Every change is a single conventional-commit scoped to the
   touched file's directory, never an amend or rebase, never a force-push. The
   git history is the audit log.
5. **Iteration cap.** The loop runs at most three iterations, after which the
   agent declares defeat and surfaces the remaining failures to the human
   on-call. This prevents runaway commit loops where the LLM repeatedly
   proposes patches that don't actually resolve the underlying issue.

Two further controls, described here as planned, are now implemented:

- **PR-mode (`--require-human-approval`) — IMPLEMENTED.** Instead of committing
  directly, the agent commits to a `heal/<run-id>` branch, pushes, opens a pull
  request, and stops without re-running the pipeline. `--dry-run` is a third
  mode that writes nothing at all.
- **Deployment-key scope:** the CI container in which the agent runs should have
  a deploy key scoped to a single IaC repo, with read-only SSH keys, and no
  access to production secrets. The agent never needs production secrets — it
  only edits YAML and runs git.

---

## 9. Success Metrics & KPIs

> **Measured vs target.** Only MTTR has a measured figure. The others remain
> targets: the perturbation suite is the harness that would produce a heal-rate
> number, but no realistic corpus exists to run it against yet, and no run has
> been made with a live LLM provider to measure patch acceptance. They are
> listed as targets and should not be quoted as results.

| KPI                              | Target (v0.1)                          | Instrumentation                  |
|----------------------------------|----------------------------------------|-----------------------------------|
| MTTR for class-N failures         | ≤ 60 seconds end-to-end (**measured: ~0.3s**) | Transcript timestamps      |
| Autonomous heal rate             | ≥ 80% of class-N failures (**not yet measured**) | Transcript `final_status` field |
| LLM-originated patch acceptance  | ≥ 70% of patches used (not fallback)   | Transcript `_fallback_reason`      |
| Regression rate                  | ≤ 2% of heals cause a new failure class| Post-heal pipeline re-run log scan|
| Mean iterations to green         | ≤ 2.0 for class-N failures             | `HealResult.iterations` field      |

The KPIs are intentionally conservative for v0.1 because the MVP runs against a
simulated pipeline; production targets will be tightened after the first quarter
of real-fleet data. The regression-rate KPI is the most important guardrail: if
the agent's patches start causing new failure classes (e.g. renaming a host that
breaks a downstream dashboard), the agent must be paused and the playbook's
coupling audited.

---

## 10. Risks & Mitigations

**Primary risk — LLM hallucination.** The LLM proposes a patch that looks
plausible but does not actually fix the failure, or worse, introduces a new
failure. *Mitigation:* YAML-validation layer (rejects malformed patches),
iteration cap (limits blast radius), conventional-commit audit trail (makes
rollback trivial), fallback diagnoser (ensures convergence even when LLM is
consistently wrong).

**Secondary risk — False-positive patching.** The agent diagnoses a failure as
a routine class-N failure when it is actually a novel failure that happens to
share a signature. *Mitigation:* conservative classification — the LLM prompt
explicitly instructs the model to set `failure_type=other` for any failure that
does not cleanly match one of the three supported classes, and the fallback
diagnoser does the same.

**Tertiary risk — Scope creep.** The agent is asked to handle increasingly
novel failures and gradually turns into a generic Ansible-autopilot, at which
point the safety model no longer holds. *Mitigation:* explicit non-goals
(§4.2) and a hard-coded allow-list of failure classes in `diagnoser.py`; adding
a new class requires a code change and a review.

**Fourth risk — LLM-provider dependency. RESOLVED.** The agent originally
depended on the `z-ai` CLI, which coupled it to one vendor's container: for
every other reader the LLM path was simply unreachable and the agent silently
ran deterministic. `agent/llm.py` now abstracts the provider (Anthropic,
OpenRouter, z-ai), auto-detects, and reports which one produced each
diagnosis in the transcript.

---

## 11. Rollout Plan & Roadmap

- **v0.1 (now):** Simulated Ansible runner, three failure scenarios, local
  commits, Markdown transcript. Intended for demo, evaluation, and team
  alignment — not production traffic.
- **v0.2 — DELIVERED (except the Actions trigger):** `PIPELINE_RUNNER=real`
  calls real `ansible-playbook` from PATH against a localhost inventory with
  intentionally broken plays, covered by `tests/test_real_ansible.py`. The
  agent does not yet trigger pipelines via the `gh` CLI or poll run status.
- **v0.3 — PR-mode DELIVERED:** the agent opens a pull request with its
  proposed patch and stops. Slack notifications are still outstanding.
- **v1.0 (16 weeks):** Production release — multi-region support, audit dashboard
  aggregating transcripts across the fleet, OpenTelemetry instrumentation, formal
  evaluation rubric for new failure classes. First release enabled by default
  for new repos; existing repos opt in via a config file in their IaC tree.

---

## 12. Appendix: Glossary & References

### 12.1 Glossary

- **Heal loop** — the agent's main control loop: run pipeline, scan failures,
  diagnose each, patch each, commit each, re-run pipeline, repeat until green
  or iteration cap.
- **Fallback diagnoser** — the deterministic rule-based diagnoser that handles
  the three supported failure classes without invoking the LLM. Used when the
  LLM is unavailable or returns malformed output.
- **Conventional commit** — a git commit message following the Conventional
  Commits specification (e.g. `fix(inventory): rename host to match playbook
  expectation`), with a type, scope, and summary.
- **Allowed paths** — the set of file globs the patcher is permitted to write
  to, controlled by the `ANSIBLE_HEAL_ALLOWED_PATHS` environment variable.
  Defaults to `ansible/**`.
- **Iteration cap** — the maximum number of heal-loop iterations, hard-coded to
  3 in v0.1. Prevents runaway commit loops.

### 12.2 References

- Conventional Commits 1.0.0 specification — conventionalcommits.org
- ansible-core 2.19 module index and deprecation notices — docs.ansible.com
- Anthropic Messages API — docs.anthropic.com
- OpenRouter API reference — openrouter.ai/docs
- asciinema / termtosvg — the demo recording toolchain
