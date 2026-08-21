# Verification report

**Date:** 2026-08-21
**Commit under test:** the tip of this branch
**Method:** fresh clone, `pip install -r requirements.txt`, commands run as shown.

This replaces the previous `UAT-report.md`, which reported 16/16 PASS against
the one seeded baseline the agent was tuned for, cited a commit SHA that is not
in this repository's history, and leaked a build-container path. Its
conclusion — *"the fallback path works identically to the LLM path"* — was
true only because both were being handed the single input the fallback was
hardcoded for.

## Suite

```console
$ ruff check .
All checks passed!

$ python3 -m pytest -q
149 passed

$ python3 -m pytest -q --cov=agent --cov=pipeline --cov=scenarios
TOTAL  1448  249  510  95  80%
```

| file | coverage |
|---|---|
| `agent/config.py` | 98% |
| `agent/llm.py` | 97% |
| `agent/committer.py` | 96% |
| `pipeline/runner.py` | 87% |
| `agent/yaml_edit.py` | 83% |
| `agent/cli.py` | 83% |
| `agent/log_scanner.py` | 81% |
| `agent/patcher.py` | 79% |

## End to end

```console
$ make demo
[1/4] Seeding the broken baseline into scratch workspace .demo-workspace/ ...
      3 failures seeded. HEAD = 2a865143f0b3
[2/4] LLM bridge: DISABLED (no provider configured) — using the deterministic
      fallback diagnoser.
[3/4] Running heal loop (max 3 retries) ...
      → success=True  iterations=1  final_exit=0  elapsed=0.3s
      Commits the agent landed:
        b2dd134 fix(inventory): rename host to match playbook expectation
        3ba7584 fix(vars): add missing variable to group_vars
        96e1b06 fix(playbook): migrate deprecated module to modern equivalent
        2a86514 chore: reset to broken baseline
```

`docs/demo.cast` is the asciinema recording of this command and `docs/demo.svg`
is it rendered. The SHAs in them are the ones that run produced.

## Generalisation

The single most important result, because the previous implementation could not
do it. `tests/test_perturbation.py` varies the undefined-variable name, the
inventory/playbook host-name pair, and the module, and requires convergence for
every combination — 38 cases.

The specific regression that motivated the suite (rename `nginx_port` to
`app_port`, `web-server-01` to `web-node-01`):

| | before | after |
|---|---|---|
| result | `success=False`, gave up at iteration 3 | `success=True`, converged at iteration 1 |
| group_vars written | `nginx_port: 8080` (wrong variable) | `app_port: 8080` |
| inventory | renamed correctly | renamed correctly |

## Real `ansible-playbook`

`tests/test_real_ansible.py` — 9 cases against ansible-core 2.19.12, no
simulator involved.

| case | real ansible behaviour | result |
|---|---|---|
| host pattern matches nothing | `[WARNING] Could not match supplied host pattern`, **exit 0** | detected; reported as exit 2 |
| undefined variable | `'nginx_port' is undefined`, exit 2 | detected, variable name recovered |
| unresolvable module | `couldn't resolve module/action`, **exit 4**, aborts before callbacks | detected via text scan |
| clean playbook | exit 0 | reported green, host in `succeeded_hosts` |
| full heal | — | converges; verified by re-running `ansible-playbook` directly and asserting exit 0 |

## Safety invariants

Asserted as properties, not by inspection:

- a fix targeting `agent/core.py` is refused and the file is **byte-unchanged**
- `../` traversal refused
- an empty allowlist denies every write, and the run reports *why* rather than
  failing silently
- `--dry-run` leaves the inventory byte-identical and adds **zero** commits
- `--require-human-approval` leaves the base branch at the **same SHA**, puts
  exactly three commits on `heal/<id>`, does not re-run the pipeline, and checks
  the base branch back out
- the test suite leaves `git status --porcelain` empty — enforced in CI

## Known gaps

- `MODULE_REPLACEMENTS` covers three modules.
- No OpenTelemetry (PRD NFR-5, `SHOULD`).
- No measured autonomous-heal-rate figure across a realistic corpus. The
  perturbation suite is the harness; the corpus does not exist yet, so the
  PRD's ≥80% target remains a target and is marked as one.
