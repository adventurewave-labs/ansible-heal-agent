# Ansible-Heal-Agent

An autonomous agent that watches an Ansible-style pipeline, diagnoses failures from the
log stream, patches the offending playbooks / inventory / vars in-tree, commits the fix
to git, and re-runs the pipeline — all without human intervention.

The project demonstrates how LLM-driven autonomous agents can be paired with existing
Ansible infrastructure to reduce on-call toil and MTTR for routine, well-understood
failure classes (stale hostname, renamed module, missing variable, etc.).

```
                ┌──────────────────────────────────────────────────────────┐
                │                     Pipeline (mock)                     │
                │  ansible-playbook site.yml  ──►  pipeline.log            │
                └──────────────────────────────────────────────────────────┘
                                          │ exit != 0
                                          ▼
   ┌──────────────────┐        ┌──────────────────────┐         ┌────────────────┐
   │   Log Scanner    │───────►│   LLM Diagnoser      │────────►│   Patcher      │
   │ extract_failures │        │ classify + propose   │         │ edit YAML in   │
   │  from log lines  │        │ fix as structured JSON│         │ repo tree      │
   └──────────────────┘        └──────────────────────┘         └────────────────┘
                                                                          │
                                                                          ▼
   ┌────────────────────────────────────┐                       ┌────────────────────┐
   │  Pipeline Restarter                │◄──────────────────────│   Committer        │
   │  re-run mock ansible-playbook      │                       │  git add + commit │
   │  up to N retries                   │                       └────────────────────┘
   └────────────────────────────────────┘
```

## Quick start

```bash
# 1. (optional) create venv
python3 -m venv .venv && source .venv/bin/activate

# 2. install deps
pip install -r requirements.txt

# 3. run the end-to-end demo
make demo
```

`make demo` will:

1. Reset the repo to a "broken" state (3 intentionally seeded failures).
2. Run the mock pipeline → capture failure log.
3. Invoke the agent: scan → diagnose → patch → commit → re-run, looping until green.
4. Write a Markdown transcript to `transcripts/demo-<timestamp>.md`.

## What's inside

```
ansible-heal-agent/
├── README.md                  this file
├── PRD.md                     Product Requirements Doc (mirror of /download/PRD-*.docx)
├── PLAN.md                    Implementation plan (mirror of /download/PLAN-*.docx)
├── Makefile                   demo / test / clean targets
├── pyproject.toml
├── requirements.txt
├── ansible/                   the artefacts the agent operates on
│   ├── inventory.yml
│   ├── group_vars/all.yml
│   └── playbooks/site.yml + webservers.yml + db.yml
├── pipeline/                  mock ansible-playbook runner + git helper
├── agent/                     the autonomous agent itself
│   ├── core.py                the heal loop
│   ├── llm.py                 LLM bridge (z-ai CLI under the hood)
│   ├── log_scanner.py
│   ├── diagnoser.py
│   ├── patcher.py
│   ├── committer.py
│   ├── pipeline_restarter.py
│   └── cli.py
├── scenarios/                 seedable failure scenarios for the demo
├── tests/                     pytest unit tests
└── transcripts/               demo run transcripts (gitignored)
```

## Architecture notes

- **Mock Ansible runner.** `pipeline/runner.py` emulates `ansible-playbook` by parsing
  the in-repo playbook YAML + inventory YAML and producing a log stream that mirrors
  real Ansible output for three well-known failure classes (stale hostname,
  deprecated module, missing variable). Exit code 0 = green, 2 = task failure.
  Swap-in real `ansible-playbook` by setting `PIPELINE_RUNNER=real` and installing
  `ansible-core`.
- **LLM bridge.** `agent/llm.py` shells out to the `z-ai` CLI (z-ai-web-dev-sdk).
  It uses GLM-4-Plus via `z-ai chat --prompt ... --output tmp.json` and parses the
  returned JSON. If the LLM is unavailable or returns malformed JSON, the agent
  transparently falls back to a deterministic rule-based diagnoser so the demo
  never gets stuck.
- **Heal loop.** `agent/core.py` runs at most `max_retries` (default 3) iterations.
  Each iteration: run → scan failures → diagnose → patch → commit → re-run.
  If the pipeline goes green, the loop exits early.
- **Git hygiene.** All patches go through `agent/committer.py`, which produces
  conventional-commit-style messages (`fix(inventory): rename web-01 → web-server-01`)
  so the agent's work is auditable in `git log`.
- **Transcript.** Every agent run writes a Markdown transcript with timestamps,
  LLM prompts/responses, patch diffs, git commits, and final pipeline status.

## Failure scenarios shipped

| ID                | What's wrong                                          | Agent's fix                                            |
|-------------------|-------------------------------------------------------|--------------------------------------------------------|
| `hostname_change` | Inventory lists `web-01` but playbook targets `web-server-01` | Rename host in `inventory.yml`                        |
| `module_change`   | Playbook uses removed `apt_key` module                | Migrate task to `get_url` + `apt_key` replacement      |
| `missing_var`     | Playbook references `{{ nginx_port }}` undefined      | Add `nginx_port: 8080` to `group_vars/all.yml`         |

## Safety & scope

The MVP is **intentionally simulated**. To run against a real Ansible fleet:

1. Set `PIPELINE_RUNNER=real` (calls `ansible-playbook` from PATH).
2. Constrain the agent's write surface via the `ANSIBLE_HEAL_ALLOWED_PATHS` env var
   (comma-separated globs the patcher is allowed to edit).
3. Enable `--require-human-approval` mode: the agent opens a PR but does not
   re-run the pipeline until a human approves.
4. Run in a CI container with read-only SSH keys and a deploy key scoped to the
   IaC repo only.

See `PRD.md` § "Non-goals" and `PLAN.md` § "Production hardening" for the
full path to prod.
