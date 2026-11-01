# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [0.3.0] — 2026-11-01

### Fixed
- **World-writable checkouts no longer defeat the inventory probe.**
  ansible-core refuses to read `ansible.cfg` from a world-writable
  directory (the default situation for `/tmp` clones and many CI
  sandboxes), answers from its default inventory instead, and the probe
  read "No inventory was parsed" as unanswerable — so the host-rename
  class declined on repositories it heals fine at a normal path. When the
  ignored-cfg warning is present, the probe now retries `ansible-inventory`
  with the cfg's own configured sources passed explicitly (`-i` per
  source), which recovers the complete host list without executing any
  repo-supplied inventory code. The retry is restricted to static sources;
  an executable script or plugin config is still declined rather than run.
- **Distrust scoped to the ignored cfg only.** An answer produced behind
  an ignored `ansible.cfg` is discarded only when the probe actually
  relied on that cfg; an attempt that carried the conventional inventory
  explicitly never depended on it and is trusted. A partial view of the
  configured sources still declines — a partial view is not grounds for
  renaming a host.
- Probe-output matching now unwraps ansible-core's terminal-width word
  wrapping first; a warning split across lines (long checkout paths in
  CI) no longer slips past the "no inventory parsed" detection.

### Changed
- Module-class test expectations adapt to the installed collections, and
  CI grew a dedicated `community.docker` leg with two-sided precondition
  asserts, so the converged branches of the module class are exercised
  every run instead of only where the collection happens to be present.

### Added
- Quick-start notes for `--playbook` and the fresh-box demo; animated SVG
  banner.

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

[0.3.0]: https://github.com/adventurewave-labs/ansible-heal-agent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/adventurewave-labs/ansible-heal-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/adventurewave-labs/ansible-heal-agent/releases/tag/v0.1.0
