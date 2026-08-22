# ansible-heal-agent

An agent that watches an Ansible pipeline, diagnoses the failures it produces,
patches the offending inventory / playbooks / vars, commits the fix, and re-runs
— for a small set of well-understood failure classes.

<p align="center">
  <img src="docs/demo.svg" alt="ansible-heal-agent healing a broken baseline" width="820">
</p>

<p align="center">
  <em>A real <code>asciinema</code> recording of <code>make demo</code>: three seeded
  failures, three conventional commits, pipeline green. Not a mock-up — the
  SHAs in the <code>git log</code> are the ones that run produced.</em>
</p>

[![CI](https://github.com/adventurewave-labs/ansible-heal-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/adventurewave-labs/ansible-heal-agent/actions/workflows/ci.yml)

## What it does, precisely

Three failure classes, against **real `ansible-playbook`** or against a bundled
simulator:

| class | detected via | fix |
|---|---|---|
| host pattern matches nothing | callback plugin — real Ansible logs a *warning* and exits **0** | rename the closest inventory entry |
| undefined variable | callback plugin (name extracted) | define it in `group_vars` with an inferred default |
| unresolvable / removed module | text scan — parse errors abort before callbacks fire (exit 4) | swap the module, carrying its arguments over |

Anything else is reported, not guessed at. See
[Where it declines](#where-it-declines).

## Quick start

```bash
pip install -r requirements.txt
make demo
```

`make demo` seeds a broken baseline into a disposable `.demo-workspace/`, heals
it, and prints the commits it landed. It does **not** write to this checkout.

Against your own repository:

```bash
# See what it would do. Writes nothing, commits nothing.
ansible-heal run --repo ~/infra --dry-run

# Open a PR and stop. Your base branch is not touched.
ansible-heal run --repo ~/infra --require-human-approval

# Apply directly (what the demo does).
ansible-heal run --repo ~/infra
```

## Safety model

These are implemented and tested, not planned.

**Write allowlist.** The patcher refuses any target outside
`ANSIBLE_HEAL_ALLOWED_PATHS` (default `ansible/**`), checked before the file is
touched at all. Since the target path can come from an LLM, this is what stops a
diagnosis proposing `agent/core.py` from being applied.

```console
$ ansible-heal run --repo ~/infra --allowed-paths 'infra/**' --dry-run
write surface: ['infra/**']
BLOCKED: refusing to write ansible/inventory.yml: outside the allowed write
surface ['infra/**']. Set ANSIBLE_HEAL_ALLOWED_PATHS to widen it.
```

**Three modes.**

| mode | writes | commits | re-runs pipeline | touches base branch |
|---|---|---|---|---|
| `--dry-run` | no | no | no | no |
| `--require-human-approval` | yes | `heal/<run-id>` | **no** | no |
| default | yes | current branch | yes | yes |

Approval mode commits to a new branch, pushes, opens a PR via `gh` if present,
checks your branch back out, and stops. The pipeline is deliberately not re-run:
the point is to hand a human a reviewable change, not to self-certify.

**Every patch is validated before it is written.** If the result is not
parseable YAML, nothing is written and the agent falls back or reports.

**Every run leaves a transcript** — failures, diagnoses, diffs, commit SHAs,
final status. [Example](docs/example-transcript.md).

## Where it declines

A diagnoser that always produces a patch is indistinguishable from one that
produces a wrong patch. These cases report and stop:

```
no inventory host resembles 'web-server-01' (inventory has ['db-primary']);
renaming an unrelated host would be a guess, so no fix is proposed
```

- no inventory host close enough to the one the play targets
- a module with no known replacement in `MODULE_REPLACEMENTS`
- a variable that is already defined (it will not be overwritten)
- any failure class it has no rule for

A run that patches nothing stops immediately rather than burning its retry
budget re-running a pipeline that cannot change.

## Configuration

| variable | default | meaning |
|---|---|---|
| `ANSIBLE_HEAL_REPO_ROOT` | this checkout | repository to operate on (`--repo`) |
| `ANSIBLE_HEAL_ALLOWED_PATHS` | `ansible/**` | comma-separated globs the patcher may write |
| `PIPELINE_RUNNER` | `mock` | `real` runs `ansible-playbook`; errors if it is not installed |
| `ANSIBLE_HEAL_LLM_PROVIDER` | auto | `anthropic` \| `openrouter` \| `z-ai` |
| `ANSIBLE_HEAL_LLM_MODEL` | per provider | model override |

## The LLM part

`agent/diagnoser.py` asks a model for a structured fix and validates it against
the same gates as everything else; if the model is unavailable, returns
malformed JSON, or proposes a patch that does not apply, the agent falls back to
the deterministic rules and **says so in the transcript**.

Providers are auto-detected: `ANTHROPIC_API_KEY`, then `OPENROUTER_API_KEY`,
then a `z-ai` CLI on PATH. HTTP is `urllib` from the standard library — no
provider SDK is required. API keys are redacted from any error the bridge
raises, because transcripts get committed and uploaded.

**With no provider configured the agent is fully deterministic**, and the demo
says so rather than implying a model was involved.

## The two runners

`PIPELINE_RUNNER=real` shells out to `ansible-playbook` and collects failures
from two sources, because neither sees everything: a
[callback plugin](pipeline/callback_plugins/heal_json.py) for runtime events,
and a text scan for parse-time errors, which abort before any callback fires.

Exit codes are interpreted by what the pipeline *means*: a play skipped because
its host pattern matched nothing exits 0 in real Ansible, and is reported here
as a failure. A deployment that silently configured zero hosts is not healthy.

The default `mock` runner (`pipeline/runner.py`) parses the playbooks and
inventory itself and emits Ansible-shaped logs. It exists so the demo and the
bulk of the suite run in under a second with no Ansible installed. It is a
simulator and is labelled as one.

## Tests

```bash
make test          # 149 tests
make lint
```

- `tests/test_perturbation.py` varies the variable name, host names and module
  across 38 cases and requires convergence in each — the agent is not tuned to
  one scenario.
- `tests/test_real_ansible.py` drives a real `ansible-playbook` process and
  asserts the end state by running the binary again afterwards.
- `tests/test_safety.py` asserts the allowlist and PR-mode invariants directly:
  the file is byte-unchanged, the base branch is at the same SHA.
- The suite runs entirely against scratch repos under `tmp_path`; a guard
  fixture fails any test that dirties this checkout.

## Roadmap

Honest list of what is **not** here:

- more failure classes (this handles three)
- `MODULE_REPLACEMENTS` covers `apt_key`, `docker`, `docker_container`
- OpenTelemetry spans (PRD NFR-5, `SHOULD`, not implemented)
- a measured autonomous-heal-rate figure across a realistic corpus — the
  perturbation suite is the harness for it, the corpus does not exist yet

See [PRD.md](PRD.md) for requirement-by-requirement status and
[PLAN.md](PLAN.md) for how it was built.
