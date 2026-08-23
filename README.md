# ansible-heal-agent

An agent that watches an Ansible pipeline, diagnoses the failures it produces,
patches the offending inventory / playbooks / vars, commits the fix, and re-runs
— for a small set of well-understood failure classes.

<p align="center">
  <img src="docs/demo.svg" alt="ansible-heal-agent healing a broken baseline" width="820">
</p>

<p align="center">
  <em>Real, unedited output from <code>make demo</code>, captured with
  <code>asciinema</code> via a scripted driver: three seeded failures, three
  conventional commits, pipeline green. The SHAs in the <code>git log</code> are
  the ones that run produced.</em>
</p>

[![CI](https://github.com/adventurewave-labs/ansible-heal-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/adventurewave-labs/ansible-heal-agent/actions/workflows/ci.yml)

## What it does, precisely

Three failure classes, against **real `ansible-playbook`** or against a bundled
simulator:

| class | detected via | fix | verified against real Ansible |
|---|---|---|---|
| host pattern matches nothing | callback plugin — real Ansible logs a *warning* and exits **0** | rename the closest inventory entry | heals to green |
| undefined variable | callback plugin (name extracted) | define it in `group_vars` with an inferred default | heals to green |
| unresolvable / removed module | text scan — parse errors abort before callbacks fire (exit 4) | swap the module for its modern equivalent | rewrite verified; see below |

The first two are asserted end to end in `tests/test_real_ansible.py`: real
`ansible-playbook` fails, the agent patches, and the binary is run again and
exits 0.

The module class is honest about where it stops. The agent rewrites the module
name and the run no longer fails on the old one — but the replacement for
`docker` / `docker_container` lives in the `community.docker` **collection**, so
the play only reaches green once that collection is installed. Installing it is
the operator's call, not the agent's, and the stall detector stops the loop
rather than re-proposing the same swap. Argument carry-over is per-module: the
`docker` mappings pass the task's arguments through unchanged, while `apt_key`
→ `get_url` deliberately rewrites them, because a keyring fetch does not take
the same arguments as a key import.

Anything else is reported, not guessed at. See
[Where it declines](#where-it-declines).

## Quick start

```bash
make install    # deps, plus the `ansible-heal` CLI on your PATH
make demo
```

`make demo` seeds a broken baseline into a disposable `.demo-workspace/`, heals
it, and prints the commits it landed. It does **not** write to this checkout.

Against your own repository:

```bash
# See what it would do. Writes nothing into ~/infra and commits nothing;
# the run log and transcript go to a scratch dir outside it, path printed.
ansible-heal run --repo ~/infra --dry-run

# Open a PR and stop. Your base branch is not touched.
ansible-heal run --repo ~/infra --require-human-approval

# Apply directly (what the demo does).
ansible-heal run --repo ~/infra
```

## Safety model

These are implemented and tested, not planned.

**The destructive step asks ansible-core, not the simulator.** Before any
inventory rename — from the deterministic diagnoser *or* from the LLM — the
agent runs `ansible <pattern> --list-hosts` and lets ansible-core resolve its
own inventory, exactly as it would for a real run: the repo's `ansible.cfg`, a
comma-separated inventory list, `ANSIBLE_CONFIG`, `ANSIBLE_INVENTORY`. If
ansible-core resolves the pattern, no rename is proposed, whatever the
simulator concluded.

Two things this gets wrong easily, both of which it got wrong before:

- Handing `ansible` the agent's own guess with `-i` overrides the very thing
  being asked about. On a repo listing two inventory files the probe saw one of
  them, answered "no such host" truthfully, and a live host in the other was
  renamed away.
- A probe that cannot answer returns "unknown", never "no". Collapsing those
  would let any environment problem re-open every hole the gate exists to close.

The probe runs with only ansible's builtin file parsers enabled and no plugin
path from the target repo, because `--dry-run` is the mode you point at a
repository you have not decided to trust, and a third-party inventory plugin is
executable code that repository supplies.

This is the single most important line of defence in the project, and it exists
because of a shape that repeated across six audit rounds: the simulator failed
to match some ordinary Ansible construct — IPv6 literals, `?` globs, whitespace
separators, nested groups, exclusion, intersection — and the diagnoser read "no
match" as "the inventory is wrong" and renamed a live host. Each of those was
fixed individually and each fix left a neighbouring case broken. Patching a
reimplementation of Ansible's pattern language toward correctness does not
converge; asking the real thing does. Where ansible-core is not installed the
guards below still apply, but the pipeline result is a simulation and the
README says so.

**What the simulator is not.** `pipeline/runner.py` is a simulator, and the
gap between it and ansible-core is where nearly every destructive bug in this
repo has come from. It now implements
union, `!` exclusion, `&` intersection, `~regex`, `*`/`?` globs, groups nested
or flat-by-`children`, IPv6 literals, and comma/semicolon/whitespace
separation, and it refuses to evaluate host ranges (`web-0[1:3]`) rather than
guessing. Where it still cannot decide, it says so and the agent declines.
`PIPELINE_RUNNER=real` hands the whole question to ansible-playbook.

One deliberate non-feature: a group mapping placed directly under `all:` is
**not** treated as a group, because ansible-core skips it (*"Skipping
unexpected key (webservers) in group (all)"*) and resolves it to zero hosts.
An earlier round added support for that shape and a test asserting it; both
were wrong, and a false green is worse than a false failure.

**One writer per repository.** Git's index is a single shared file. Two
concurrent runs interleaved `add` and `commit` badly enough that one run's
staged edit landed inside the other's commit, so apply and PR modes take an
exclusive lock on the target repo for the duration.

**Writes are atomic.** A patch goes to a temp file in the same directory and is
renamed over the target. `write_text` truncates first, so a write that failed
part-way — a full disk, a quota — left a file cut off mid-key that still parsed
as YAML, which meant nothing downstream noticed.

**Write allowlist.** The patcher refuses any target outside
`ANSIBLE_HEAL_ALLOWED_PATHS` (default `ansible/**`), checked before the file is
touched at all. Since the target path can come from an LLM, this is what stops a
diagnosis proposing `agent/core.py` from being applied.

The check runs twice: once on the path the diagnosis asked for, and again on
the path it *resolves* to. Only checking the declared name made the allowlist
bypassable by a symlink — `ansible/group_vars/all.yml` pointing at
`shared/secrets.yml` is inside the write surface by name and outside it in
fact, and the write would not even show up in git, because the committer stages
the declared path whose own bytes never changed.

```console
$ ansible-heal run --repo ~/infra --allowed-paths 'infra/**' --dry-run
repo:  /home/you/infra
mode:  dry-run
write surface: ['infra/**']
artefacts:     /tmp/ansible-heal-dryrun-z4bsk0d0  (nothing is written to the repo)

3 proposal(s); nothing written.

BLOCKED: refusing to write ansible/playbooks/webservers.yml: outside the allowed
write surface ['infra/**']. Set ANSIBLE_HEAL_ALLOWED_PATHS to widen it.

BLOCKED: refusing to write ansible/group_vars/all.yml: outside the allowed write
surface ['infra/**']. Set ANSIBLE_HEAL_ALLOWED_PATHS to widen it.

BLOCKED: refusing to write ansible/inventory.yml: outside the allowed write
surface ['infra/**']. Set ANSIBLE_HEAL_ALLOWED_PATHS to widen it.

transcript: /tmp/ansible-heal-dryrun-z4bsk0d0/transcripts/run-20260822-2103.md

Success: False  Iterations: 0  Final exit: 2
```

One line per refused write — three proposals, three refusals. Line wrapping
above is this document's; the real output does not wrap.

**Three modes.**

| mode | writes | commits | re-runs pipeline | touches base branch |
|---|---|---|---|---|
| `--dry-run` | no — not even a run log; artefacts go to a scratch dir outside the repo | no | no | no |
| `--require-human-approval` | yes | `heal/<run-id>` | **no** | no |
| default | yes | current branch | yes | yes |

Approval mode commits to a new branch, pushes, opens a PR via `gh` if present,
checks your branch back out, and stops. The pipeline is deliberately not re-run:
the point is to hand a human a reviewable change, not to self-certify.

**Every YAML patch is parsed before it is written.** If the result is not
parseable YAML, nothing is written and the agent falls back or reports. The
check is keyed on the file suffix, so it covers every file the agent currently
knows how to edit — inventory, playbooks, `group_vars` — and would not cover a
non-YAML target such as a `.j2` template if a future fix class introduced one.

**Every CLI run leaves a transcript** (unless `--no-transcript`) — failures, diagnoses, diffs, commit SHAs,
final status. [Example](docs/example-transcript.md).

## Where it declines

A diagnoser that always produces a patch is indistinguishable from one that
produces a wrong patch. These cases report and stop — on the console, not only
in the transcript:

```console
$ ansible-heal run --repo ~/infra
NO FIX: no inventory host resembles 'web-server-01' (inventory has
['db-primary']); renaming an unrelated host would be a guess, so no fix is
proposed
```

**A `hosts:` pattern that is not one stale hostname.** This is where most of
the damage lived, and every case below was a real repository the agent broke
while reporting success:

- a *group* name — an empty group (a tier scaled to zero, a dynamic-inventory
  placeholder) makes Ansible skip the play and exit 0. Renaming the nearest
  host to match deletes a live host and creates a group/host collision
- `localhost`, which Ansible always provides implicitly. Renaming a real host
  to it redirects every later localhost play at that machine
- a multi-host pattern — `web-01 db-01`, `web-01,db-01`, a YAML list. These
  resolve properly now; when one cannot, it is not a hostname to rename to
- `all` or `*` matching nothing, which means the inventory is empty
- any pattern containing `! & [ ] * ? ~` — or a `:` that is not part of an
  IPv6 address. Those are pattern syntax, not names. The check runs on the
  pattern *as the operator wrote it*, not on a fragment a runner split out of
  it: `fd00::21` reached the guard as the token `21`, looked like an ordinary
  hostname, and a real address was renamed to `fd00`

**The repository's own state.**

- ansible-vault encrypted files are never read or rewritten. The ciphertext is
  a valid YAML scalar, so a validity check alone passes it and a string-replace
  edit writes plaintext over the operator's secrets
- a hardlinked target, whose other names the write surface cannot bound
- a merge, rebase, cherry-pick, revert or bisect in progress — the agent will
  not commit into a half-finished operation and conclude it for you
- a detached HEAD, where commits would be unreachable from any branch
- a commit git itself refuses: a failing hook, a submodule, an ignored path
- a file missing, unreadable, not a regular file, or not the YAML shape its
  caller needs

**The ordinary cases.**

- no inventory host close enough to the one the play targets
- an inventory host another play still targets, where renaming it would break
  that play
- a module with no known replacement in `MODULE_REPLACEMENTS`
- a variable already defined in `group_vars/all*`, on the play, or on the task
- a variable defined only in `group_vars/<group>` or `host_vars/` — it names
  the file and stops. Adding a global default would override the operator's
  per-host value for every other host, and merging every vars file into one
  namespace to call the run green hides a play that genuinely fails on a host
  the variable was never defined for. Both answers are wrong; saying which file
  defines it is not
- any failure class it has no rule for

A refusal is a result. `heal()` returns them on `HealResult.declined`, and the
CLI prints each one.

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
The two overlap — a no-hosts warning and an undefined variable are both visible
to each — so records are de-duplicated on whatever that failure class's *fix* is
keyed on: the variable name for an undefined variable, the module for a removed
one, the pattern for a host. Keying them all on `(type, host, …)` looked right
and silently failed for variables, because the text scan's record carries no
host; the operator got two identical proposals and a spurious "already defined"
patch failure.

Exit codes are interpreted by what the pipeline *means*: a play skipped because
its host pattern matched nothing exits 0 in real Ansible, and is reported here
as a failure. A deployment that silently configured zero hosts is not healthy.

The default `mock` runner (`pipeline/runner.py`) parses the playbooks and
inventory itself and emits Ansible-shaped logs. It exists so the demo and the
bulk of the suite run in under a second with no Ansible installed. It is a
simulator and is labelled as one.

## Tests

```bash
make test          # 228 tests
make lint
```

- `tests/test_perturbation.py` — 38 tests, of which **26 require convergence**
  across varied variable names, host names and modules, so the agent is not
  tuned to one scenario. The other 12 are the counterweight: 8 pin the inferred
  defaults and 4 assert the agent *refuses* rather than guessing.
- `tests/test_real_ansible.py` drives a real `ansible-playbook` process and
  asserts the end state by running the binary again afterwards — for the host
  and variable classes, that means exit 0 from ansible itself, not from our own
  bookkeeping.
- `tests/test_safety.py` asserts the allowlist and PR-mode invariants directly:
  the file is byte-unchanged, the base branch is at the same SHA.
- Every test that touches a repository runs against a scratch repo under
  `tmp_path`; the rest are pure-function tests over config and the LLM bridge.
  A guard fixture fails any test that dirties this checkout, and CI re-checks
  with `git status --porcelain` afterwards.

## Roadmap

Honest list of what is **not** here:

- more failure classes (this handles three)
- `MODULE_REPLACEMENTS` covers `apt_key`, `docker`, `docker_container` — and
  note that on ansible-core 2.19 `apt_key` still *resolves*, so that mapping is
  a modernisation rather than a fix for a broken play
- converging the module class against real Ansible, which needs the replacement
  collection present
- OpenTelemetry spans (PRD NFR-5, `SHOULD`, not implemented)
- a measured autonomous-heal-rate figure across a realistic corpus — the
  perturbation suite is the harness for it, the corpus does not exist yet

See [PRD.md](PRD.md) for requirement-by-requirement status and
[PLAN.md](PLAN.md) for how it was built.
