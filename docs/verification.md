# Verification report

**Date:** 2026-08-23
**Commit under test:** the tip of this branch
**Method:** fresh clone, `make install`, commands run as shown and their real
output pasted back. Every number below was measured on the run that produced
this file; where a figure is not measurable here it says so instead of
guessing.

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
245 passed

$ python3 -m pytest -q --cov=agent --cov=pipeline
TOTAL  2287 375 934 172 81%
```

That is the command CI runs, and the one its `--cov-fail-under=78` gate applies
to. Adding `--cov=scenarios` gives `TOTAL 2418 409 992 182 81%` — the previous
version of this file printed the two-package totals under the three-package
command, which is exactly the kind of thing this document exists to catch.

| file | coverage |
|---|---|
| `agent/config.py` | 95% |
| `agent/llm.py` | 97% |
| `agent/pipeline_restarter.py` | 90% |
| `pipeline/runner.py` | 86% |
| `agent/committer.py` | 85% |
| `agent/yaml_edit.py` | 81% |
| `agent/cli.py` | 80% |
| `agent/log_scanner.py` | 81% |
| `agent/patcher.py` | 84% |
| `agent/core.py` | 70% |
| `agent/diagnoser.py` | 75% |
| `pipeline/git_helper.py` | 81% |

The three lowest are named deliberately rather than omitted: `core.py`'s
uncovered lines are mostly `Transcript` formatting, `git_helper.py` is the
thinnest-tested module in the repo, and `diagnoser.py`'s LLM branch is only
exercised with a provider configured.

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
        d53fc0e 2026-08-22 fix(inventory): rename host to match playbook expectation
        dea60fc 2026-08-22 fix(vars): add missing variable to group_vars
        e879c40 2026-08-22 fix(playbook): migrate deprecated module to modern equivalent
        34d5e05 2026-08-22 chore: reset to broken baseline
```

`docs/demo.cast` is an asciinema capture of the same command from an earlier
run, and `docs/demo.svg` is that cast rendered by termtosvg. The SHAs in them
are the ones *that* run produced, so they differ from the block above — the
demo reseeds from scratch each time. The capture was driven by a script rather
than typed at a live prompt: the `$ ` prefixes on the two trailing `git`
commands are printed by the driver, and the pauses between them are scripted.
The program output itself is real and unedited.

## Generalisation

The single most important result, because the previous implementation could not
do it. `tests/test_destructive_inputs.py` is the most important file in the suite:
sixty-three repositories the agent must not damage. Every one is a case where an
earlier version made a confident, committed change that destroyed something —
a vault-encrypted secrets file rewritten as plaintext, a production host
renamed out of the inventory to match a group name, an operator's half-finished
merge concluded under a commit subject about something else. The required
behaviour in nearly all of them is identical: refuse, say why, change nothing.
The exceptions are the ones that must still *heal* — a stale IPv6 address, a
`?` glob — where the point is that a guard added to stop the damage did not
also stop the fix.

`tests/test_runner_inputs.py` is the counterpart to this section: sixteen cases for
repositories that are *not* the seeded baseline — comma-separated and
list-valued `hosts:`, flat inventory groups, absent group_vars, unparseable
playbooks, a missing imported playbook, an unsupported pattern, and a variable
set at play level. Every one of them used to end in a traceback, a crash, or a
confident "fix" applied to a working repository.

`tests/test_perturbation.py` varies the undefined-variable name, the
inventory/playbook host-name pair, and the module. It is **37 tests, of which
25 require convergence** — 8 variable names, 4 host pairs, 1 module and 12
combinations. Only one module now: the agent asks `ansible-doc` before
migrating anything and refuses to rewrite a playbook whose module still
resolves, so `apt_key` no longer belongs in a suite asserting convergence. The other 12 are the counterweight: 8 pin the inferred defaults
and 4 assert the agent refuses rather than guessing.

The specific regression that motivated the suite, reproduced ad hoc rather than
as a fixture (rename `nginx_port` to `app_port` and the host pair away from the
seeded names):

| | before | after |
|---|---|---|
| result | `success=False`, gave up at iteration 3 | `success=True`, converged at iteration 1 |
| group_vars written | `nginx_port: 8080` (wrong variable) | `app_port: 8080` |
| inventory | renamed correctly | renamed correctly |

## Real `ansible-playbook`

`tests/test_real_ansible.py` — 16 tests against ansible-core 2.19.12, no
simulator involved.

| case | real ansible behaviour | result |
|---|---|---|
| host pattern matches nothing | `[WARNING] Could not match supplied host pattern`, **exit 0** | detected; reported as exit 2 |
| undefined variable | `'nginx_port' is undefined`, exit 2 | detected, variable name recovered |
| unresolvable module | `couldn't resolve module/action`, **exit 4**, aborts before callbacks | detected via text scan |
| clean playbook | exit 0 | reported green, host in `succeeded_hosts` |
| full heal, host class | — | converges; verified by re-running `ansible-playbook` directly and asserting exit 0 |
| full heal, variable class | — | converges; same direct re-run check |
| module class | — | swap lands and Ansible stops looking for the old module. Does **not** reach green: `community.docker` is a Galaxy collection, and installing it is the operator's call. The stall detector is asserted to stop rather than re-propose |

`apt_key`, the module in the seeded scenario, is **not** part of the real-Ansible
evidence: on 2.19 it still resolves, so the mock's parse-time rejection of it is
simulation. `docker` is what the real checks use.

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
- `--dry-run` writes nothing into the target repo at all — not a run log, not a
  transcript; `git status --porcelain` in the target is byte-identical before
  and after, and the artefacts land in a scratch dir outside it
- a rename that would break another play still targeting the host is refused,
  so two contending plays cannot make the agent rename an entry back and forth
  and land a junk commit per iteration
- the callback and the text scan de-duplicate per failure class, so one failure
  yields one diagnosis
- the write allowlist is re-checked on the **resolved** path, so a symlink
  inside the write surface cannot smuggle a write outside it
- `--dry-run` does not create a git repository in a target that had none, and
  does not commit — `heal()` called as a library gets the same guarantee, not
  just the CLI
- a declined diagnosis is not a commit: PR mode with nothing to fix neither
  pushes a branch nor reports success, and the retry budget is not spent
- PR mode refuses a repository with no commits rather than branching off an
  unborn HEAD and losing the base branch
- a host pattern the simulator cannot evaluate is reported as such, not as a
  missing host, so the diagnoser never "fixes" a repository that was green
- ansible-vault encrypted files are never read or rewritten, and a hardlinked
  target is refused — the write surface cannot bound where those bytes land
- patches are written atomically, so a failed write cannot truncate the original
- the agent refuses to commit into a repository that is mid-merge, mid-rebase or
  on a detached HEAD, and reports a commit git refuses rather than counting it
- apply and PR modes hold an exclusive lock on the target repo: 6/6 paired
  concurrent runs left nothing staged, against 9/12 that stranded work before
- a worktree and a submodule are recognised as repositories — `.git` is a file
  there, and reading that as "not a repository" made the agent `git init` on
  top of a real one
- initialising a directory that is not a repository does not commit what was
  already in it
- host patterns are checked as the operator wrote them, not as a runner split
  them: an IPv6 literal is one host, and `?` is a glob
- the test suite leaves `git status --porcelain` empty — enforced in CI

## Known gaps

- `MODULE_REPLACEMENTS` covers three modules.
- No OpenTelemetry (PRD NFR-5, `SHOULD`).
- No measured autonomous-heal-rate figure across a realistic corpus. The
  perturbation suite is the harness; the corpus does not exist yet, so the
  PRD's ≥80% target remains a target and is marked as one.
