# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [0.2.0] — 2026-09-29

### Added
- **Missing-collection failure class.** When a module's collection is not
  installed, the agent checks with `ansible-galaxy` and declares it in the
  repo's collections requirements file as a `fix(deps)` commit. It never
  installs anything; the next iteration stops with the exact install command.
  Declines when the collection is installed but lacks the module, when
  `ansible-galaxy` cannot answer, on a legacy roles-list file, or on an invalid
  name.
- **`ansible-heal eval`** (`make eval`): heal rate, false-fix rate and decline
  precision over a deterministic 20-case corpus, each case in a throwaway repo.
  JSON + Markdown report; CI gates at ≥90% heal and zero false fixes.
- **SARIF 2.1.0 output**: `run --dry-run --sarif PATH`, rules AHA001–AHA004 and
  AHA900, located at the offending playbook line with the proposed diff.
  `--fail-on-findings` for gating.
- **Composite GitHub Action** (`action.yml`): read-only dry-run scan that
  uploads to code scanning. Use `adventurewave-labs/ansible-heal-agent@v0`.
- **Read-only MCP server**: `ansible-heal mcp` exposes `diagnose`,
  `explain_decline` and `list_failure_classes` over stdio. No apply tool.
- **OpenTelemetry spans** (PRD NFR-5) with GenAI semantic conventions,
  including token and prompt-cache usage. Optional `[otel]` extra; no-op when
  absent or with `ANSIBLE_HEAL_OTEL=0`.

### Changed
- LLM diagnoses use **schema-constrained structured output** (forced tool call
  on Anthropic, strict `json_schema` on OpenRouter) and are re-validated
  locally. The Anthropic system prompt is marked for prompt caching.
- Client errors (400/401/403…) from an LLM provider fail immediately; only
  408/409/425/429/5xx/529 are retried.
- Default models: `claude-sonnet-5` (Anthropic), `anthropic/claude-sonnet-5`
  (OpenRouter). Override with `ANSIBLE_HEAL_LLM_MODEL`.
- The seeded demo now lands **4 commits** instead of 3 — the extra one declares
  `community.docker`.

### Fixed
- ansible-core 2.17's "None of the provided paths were usable" from
  `ansible-galaxy collection list` is read as "not installed", not as
  unanswerable.

## [0.1.0]

Initial release: three failure classes (host pattern, undefined variable,
removed module), dry-run / PR / apply modes, write allowlist, ansible-core as
the authority for every destructive decision.

[0.2.0]: https://github.com/adventurewave-labs/ansible-heal-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/adventurewave-labs/ansible-heal-agent/releases/tag/v0.1.0
