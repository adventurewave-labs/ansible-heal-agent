# SOTA improvement loops

Scheduled improvement run on branch `sota/improvement-loops`: loop 0 plus five
follow-up loops, one backlog item each. Every item lands with tests, keeps
`pytest` and `ruff` green, and is ticked here with a log line.

## Backlog

- [x] **L0 — Structured outputs.** Diagnosis contract as JSON Schema; Anthropic
  forced tool call + prompt caching on the system prompt, OpenRouter strict
  `json_schema`, local validation for every provider; 4xx fail fast, 408/429/5xx
  retried; default model bumped to `claude-sonnet-5`.
- [x] **L1 — OpenTelemetry.** GenAI semantic-convention spans around heal loop,
  diagnose, LLM call, patch, commit, pipeline run. Optional dependency; no-op
  when `opentelemetry-api` is absent. Closes PRD NFR-5.
- [x] **L2 — Heal-rate eval harness.** `ansible-heal eval` over a generated
  perturbation corpus: heal rate, false-fix rate, decline rate, iterations —
  JSON + Markdown report.
- [ ] **L3 — Missing-collection class.** When a module resolves only via an
  uninstalled collection, declare it in `collections/requirements.yml`
  (ansible-galaxy format) instead of stalling red; declines when already
  declared or ambiguous.
- [ ] **L4 — SARIF + GitHub Action.** SARIF 2.1.0 for proposals and declines;
  composite `action.yml` running dry-run and uploading to code scanning.
- [ ] **L5 — MCP server.** Read-only stdio MCP server exposing diagnose,
  dry-run and explain-decline as tools. Final docs/PR pass.

## Log

- L0 — structured outputs shipped; 12 new tests (`tests/test_structured_output.py`).
- L1 — OpenTelemetry spans shipped (`agent/telemetry.py`, `[otel]` extra, GenAI semconv incl. token + cache-read usage); PRD NFR-5 → implemented; 7 new tests.
- L2 — `ansible-heal eval` / `make eval` shipped (`agent/evaluation.py`): 20-case corpus (16 heal, 4 must-decline), heal 100%, false-fix 0%, decline precision 100% on the deterministic path; gated CI job with job-summary table; 9 new tests.
