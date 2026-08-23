"""The agent's heal loop.

Three modes, selected by the ``mode`` argument:

``apply`` (default)
    iter 0: run pipeline → if green, exit. else collect failures.
    iter 1..max_retries: diagnose → patch → commit each failure, re-run.
    Exit early on green; give up after max_retries.

``dry-run``
    Run once, diagnose every failure and compute the patch it *would* apply,
    write nothing into the target repository, commit nothing. This is the mode
    to point at a repository you do not yet trust the agent with, so "write
    nothing" is meant literally: run logs and the transcript are redirected to a
    scratch directory outside the repo (see ``config.set_output_root``) rather
    than being dropped into ``pipeline/runs/`` and ``transcripts/``.

``pr``
    PRD NFR-3. Patch and commit onto a fresh ``heal/<run-id>`` branch, push it,
    open a pull request, and **stop** — the pipeline is not re-run and the base
    branch is never touched. A human merges, or does not.

Only ``apply`` writes to the checked-out branch, and only ``apply`` re-runs the
pipeline after patching.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from agent import committer, config, diagnoser, patcher, pipeline_restarter
from agent import llm as llm_bridge
from agent.config import repo_root
from pipeline import git_helper

#: Modes the heal loop understands.
MODE_APPLY = "apply"
MODE_DRY_RUN = "dry-run"
MODE_PR = "pr"
MODES = (MODE_APPLY, MODE_DRY_RUN, MODE_PR)


@dataclass
class IterationRecord:
    iteration: int
    run_log_path: str
    exit_code: int
    failures: list[dict] = field(default_factory=list)
    diagnoses: list[dict] = field(default_factory=list)
    patches: list[dict] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)


@dataclass
class Proposal:
    """A fix the agent would apply, in dry-run mode."""
    failure: dict
    diagnosis: dict
    target_file: str | None
    diff: str
    blocked_reason: str | None = None


@dataclass
class HealResult:
    success: bool
    iterations: int
    final_exit_code: int
    history: list[IterationRecord] = field(default_factory=list)
    transcript_path: str | None = None
    mode: str = MODE_APPLY
    proposals: list[Proposal] = field(default_factory=list)
    branch: str | None = None
    pushed: bool = False
    push_error: str | None = None
    pr_url: str | None = None
    blocked: list[str] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)

    def decline(self, reason: str) -> None:
        """Record a refusal once, however many iterations re-discover it."""
        if reason and reason not in self.declined:
            self.declined.append(reason)


def heal(playbook: str = "ansible/playbooks/site.yml",
         max_retries: int = 3,
         use_llm: bool = True,
         transcript: Transcript | None = None,
         mode: str = MODE_APPLY,
         remote: str = "origin") -> HealResult:
    """Run the heal loop in ``mode``. Returns a HealResult summarising the run."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")

    history: list[IterationRecord] = []
    result = HealResult(success=False, iterations=0, final_exit_code=-1,
                        history=history, mode=mode)

    if mode == MODE_DRY_RUN:
        # Deliberately before init_if_needed(): that helper runs `git init`,
        # `git add .` and a commit when the target is not a repo, which turned
        # `--dry-run` — the mode for a repository you have not decided to trust
        # the agent with — into a mode that created one and swept every
        # unrelated file present into a commit.
        #
        # The artefact redirect lives here rather than only in the CLI, so
        # calling heal(mode="dry-run") as a library gets the same promise.
        with _dry_run_artefacts():
            return _heal_dry_run(playbook, use_llm, transcript, result)

    git_helper.init_if_needed()

    if mode in (MODE_APPLY, MODE_PR):
        blocker = committer.commit_guard()
        if blocker:
            result.decline(blocker)
            result.push_error = blocker
            return result
        # One writer per repository: git's index is a single shared file, and
        # two concurrent runs interleaved add/commit badly enough that one
        # run's staged edit landed inside the other's commit.
        try:
            with git_helper.exclusive_lock():
                return _heal_locked(playbook, max_retries, use_llm, transcript,
                                    result, mode, remote)
        except git_helper.GitStateError as e:
            result.decline(str(e))
            result.push_error = str(e)
            return result

    return _heal_locked(playbook, max_retries, use_llm, transcript, result,
                        mode, remote)


def _heal_locked(playbook, max_retries, use_llm, transcript, result, mode,
                 remote):
    """The heal loop proper, once the repository lock (if any) is held."""
    if mode == MODE_PR:
        return _heal_pr(playbook, use_llm, transcript, result, remote)
    return _heal_apply(playbook, max_retries, use_llm, transcript, result)


def _diagnose_and_patch(failure: dict, use_llm: bool, transcript: Transcript | None,
                        rec: IterationRecord, dry_run: bool = False):
    """Diagnose one failure and apply (or simulate) its patch.

    Returns ``(fix, diagnosis, patch)`` on success, or ``(None, diagnosis, None)``
    if no patch could be applied. Falls back from an LLM diagnosis to the
    deterministic one exactly once.
    """
    if transcript:
        transcript.failure_found(failure)

    diag = diagnoser.diagnose(failure, use_llm=use_llm)
    rec.diagnoses.append(diag)
    if transcript:
        transcript.diagnosis(failure, diag)

    fix = diag.get("fix", {})
    if fix.get("action", "none") == "none":
        # An honest refusal, not a patch. apply_fix() returns ok=True for this
        # no-op, which made every caller treat it as a successful fix: PR mode
        # pushed an empty branch and reported success, the stall detector saw
        # "progress", and commit_fix's empty return was recorded as a SHA.
        reason = diag.get("_no_fix_reason") or "no automated fix is available"
        if transcript:
            transcript.declined(failure, reason)
        return None, diag, {"declined_reason": reason}

    try:
        patch = patcher.apply_fix(fix, dry_run=dry_run)
        rec.patches.append(patch)
        if transcript:
            transcript.patch_applied(fix, patch)
        return fix, diag, patch
    except patcher.PathNotAllowed as e:
        # Never retried: a denied path is a policy decision, not a bad guess.
        # `fix` is deliberately returned as None so no caller commits it, but
        # the target file still has to reach the report — otherwise the CLI
        # renders "BLOCKED None:" and the operator cannot tell which write was
        # refused.
        if transcript:
            transcript.patch_blocked(fix, str(e))
        return None, diag, {"blocked_reason": str(e),
                            "target_file": (fix or {}).get("target_file")}
    except patcher.PatchError as e:
        if transcript:
            transcript.patch_failed(fix, str(e))
        if not (use_llm and "_fallback_reason" not in diag):
            # A vault-encrypted target, an unwritable file, a hardlink, a patch
            # that would change nothing. These existed only inside the
            # transcript, so `--no-transcript` made a run that did nothing look
            # like a run that found nothing.
            return None, diag, {"failed_reason": str(e),
                                "target_file": (fix or {}).get("target_file")}

        # The LLM produced a patch that did not apply. Try the deterministic
        # diagnoser once, then give up on this failure.
        fb_diag = diagnoser.fallback_diagnose(failure)
        fb_diag["_fallback_reason"] = f"LLM patch failed validation: {e}"
        rec.diagnoses.append(fb_diag)
        if transcript:
            transcript.diagnosis(failure, fb_diag)
        fb_fix = fb_diag.get("fix", {})
        try:
            patch = patcher.apply_fix(fb_fix, dry_run=dry_run)
            rec.patches.append(patch)
            if transcript:
                transcript.patch_applied(fb_fix, patch)
            return fb_fix, fb_diag, patch
        except patcher.PatchError as e2:
            if transcript:
                transcript.patch_failed(fb_fix, str(e2))
            return None, fb_diag, None


def _run_once(playbook: str, i: int, transcript: Transcript | None) -> tuple:
    run_id = time.strftime(f"%Y%m%d-%H%M%S-iter{i}")
    run = pipeline_restarter.restart(playbook=playbook, run_id=run_id)
    if transcript:
        transcript.iteration_header(i, run)
    rec = IterationRecord(
        iteration=i,
        run_log_path=run.log_path,
        exit_code=run.exit_code,
        failures=run.failures,
    )
    return run, rec


@contextmanager
def _dry_run_artefacts():
    """Send run logs and transcripts outside the repo, unless already redirected.

    The CLI sets an output root before calling heal() so it can print the path;
    a library caller does not, and used to get run logs written into the target
    repo by a mode documented as writing nothing.
    """
    if config.output_root_override() is not None:
        yield
        return
    scratch = tempfile.mkdtemp(prefix="ansible-heal-dryrun-")
    config.set_output_root(scratch)
    try:
        yield
    finally:
        config.set_output_root(None)


def _fix_signature(fix: dict) -> tuple:
    """Identity of a fix, for detecting one the agent has already applied."""
    return (
        fix.get("action"),
        fix.get("target_file"),
        fix.get("old") or fix.get("search") or fix.get("key"),
        fix.get("new") or fix.get("replace") or fix.get("value"),
    )


def _heal_apply(playbook, max_retries, use_llm, transcript, result) -> HealResult:
    history = result.history
    #: Signatures of fixes already applied, so a repeat is not counted as progress.
    applied: set[tuple] = set()
    for i in range(max_retries + 1):
        run, rec = _run_once(playbook, i, transcript)

        if run.exit_code == 0:
            result.success = True
            result.iterations = i
            result.final_exit_code = 0
            history.append(rec)
            if transcript:
                transcript.green(i, run)
            break

        if i == max_retries:
            result.success = False
            result.iterations = i
            result.final_exit_code = run.exit_code
            history.append(rec)
            if transcript:
                transcript.give_up(i, run)
            break

        progressed = False
        for failure in run.failures:
            fix, diag, patch = _diagnose_and_patch(failure, use_llm, transcript, rec)
            if patch and patch.get("blocked_reason"):
                result.blocked.append(patch["blocked_reason"])
            if patch and patch.get("declined_reason"):
                result.decline(patch["declined_reason"])
            if patch and patch.get("failed_reason"):
                result.decline(patch["failed_reason"])
            if fix is None:
                continue
            try:
                sha = committer.commit_fix(fix, diag)
            except git_helper.GitStateError as e:
                # A failing pre-commit hook, a submodule, an ignored path. The
                # edit is on disk and staged; saying "success" here is how the
                # agent used to leave work stranded in the index.
                result.decline(f"patched {fix.get('target_file')} but {e}")
                if transcript:
                    transcript.commit_failed(fix, str(e))
                continue
            rec.commits.append(sha)
            # A fix the agent has already applied in an earlier iteration is not
            # progress, even though it changed a file: it means two failures are
            # fighting over the same edit and the loop cannot converge. Counting
            # it as progress keeps the stall detector quiet while the agent burns
            # the retry budget and lands a junk commit per round.
            signature = _fix_signature(fix)
            if signature in applied:
                if transcript:
                    transcript.repeated_fix(signature)
            else:
                applied.add(signature)
                progressed = True
            if transcript:
                transcript.committed(sha, fix)

        history.append(rec)

        if not progressed:
            # Nothing was patched this round, so re-running would produce the
            # identical failure set. Stop instead of burning the retry budget
            # on a loop that cannot converge.
            result.success = False
            result.iterations = i
            result.final_exit_code = run.exit_code
            if transcript:
                transcript.stalled(i, run)
            break

    return result


def _heal_dry_run(playbook, use_llm, transcript, result) -> HealResult:
    """Diagnose everything, write nothing, commit nothing."""
    run, rec = _run_once(playbook, 0, transcript)
    result.history.append(rec)
    result.final_exit_code = run.exit_code
    result.iterations = 0

    if run.exit_code == 0:
        result.success = True
        if transcript:
            transcript.green(0, run)
        return result

    for failure in run.failures:
        fix, diag, patch = _diagnose_and_patch(
            failure, use_llm, transcript, rec, dry_run=True)
        blocked = patch.get("blocked_reason") if patch else None
        if blocked:
            result.blocked.append(blocked)
        declined = patch.get("declined_reason") if patch else None
        if declined:
            result.decline(declined)
        failed = patch.get("failed_reason") if patch else None
        if failed:
            result.decline(failed)
        result.proposals.append(Proposal(
            failure=failure,
            diagnosis=diag,
            target_file=((fix or {}).get("target_file")
                         or (patch or {}).get("target_file")),
            diff=(patch or {}).get("diff", ""),
            blocked_reason=blocked,
        ))

    # A dry run reports; it does not claim to have healed anything.
    result.success = False
    if transcript:
        transcript.dry_run_summary(result)
    return result


def _heal_pr(playbook, use_llm, transcript, result, remote) -> HealResult:
    """Patch onto a fresh branch, push it, open a PR, and stop (PRD NFR-3)."""
    run, rec = _run_once(playbook, 0, transcript)
    result.history.append(rec)
    result.final_exit_code = run.exit_code
    result.iterations = 0

    if run.exit_code == 0:
        result.success = True
        if transcript:
            transcript.green(0, run)
        return result

    if not git_helper.has_commits():
        # Branching off an unborn HEAD gives the base branch no commit to
        # return to, so the checkout back at the end fails and the operator is
        # left stranded on the heal branch with their base branch gone.
        result.push_error = (
            "the target repository has no commits yet; make an initial commit "
            "before using --require-human-approval")
        return result

    base = git_helper.current_branch()
    branch = f"heal/{time.strftime('%Y%m%d-%H%M%S')}"
    if not git_helper.create_branch(branch):
        result.push_error = f"could not create branch {branch}"
        return result
    result.branch = branch
    if transcript:
        transcript.branch_created(branch, base)

    try:
        _pr_patch_loop(run, use_llm, transcript, rec, result)
    except Exception:
        # Any escape here used to strand the operator on the heal branch, which
        # is the one thing PR mode promises not to do.
        git_helper.checkout(base)
        raise

    if not rec.commits:
        git_helper.checkout(base)
        result.push_error = (result.push_error
                             or "no patches applied; nothing to open a PR for")
        return result

    if git_helper.has_remote(remote):
        proc = git_helper.push(remote, branch)
        result.pushed = proc.returncode == 0
        if not result.pushed:
            result.push_error = proc.stderr.strip()[:500]
        else:
            result.pr_url = _open_pull_request(branch, base, rec)
    else:
        result.push_error = f"no remote named {remote!r}; branch left local"

    # The pipeline is deliberately NOT re-run and the base branch is not
    # touched. Success here means "a reviewable change was produced".
    result.success = bool(rec.commits)
    git_helper.checkout(base)
    if transcript:
        transcript.pr_summary(result)
    return result


def _pr_patch_loop(run, use_llm, transcript, rec, result) -> None:
    for failure in run.failures:
        fix, diag, patch = _diagnose_and_patch(failure, use_llm, transcript, rec)
        if patch and patch.get("blocked_reason"):
            result.blocked.append(patch["blocked_reason"])
        if patch and patch.get("declined_reason"):
            result.decline(patch["declined_reason"])
        if patch and patch.get("failed_reason"):
            result.decline(patch["failed_reason"])
        if fix is None:
            continue
        try:
            sha = committer.commit_fix(fix, diag)
        except git_helper.GitStateError as e:
            result.decline(f"patched {fix.get('target_file')} but {e}")
            if transcript:
                transcript.commit_failed(fix, str(e))
            continue
        rec.commits.append(sha)
        if transcript:
            transcript.committed(sha, fix)



def _open_pull_request(branch: str, base: str, rec: IterationRecord) -> str | None:
    """Open a PR with the gh CLI if it is available. Returns the URL or None."""
    if not shutil.which("gh"):
        return None
    body_lines = [
        "Opened automatically by ansible-heal-agent in `--require-human-approval` "
        "mode. The pipeline has **not** been re-run and `" + base + "` was not "
        "modified.",
        "",
        f"Failures diagnosed: {len(rec.failures)}",
        "",
    ]
    for d in rec.diagnoses:
        body_lines.append(f"- **{d.get('failure_type', 'unknown')}** — "
                          f"{d.get('diagnosis', '').strip()}")
    proc = subprocess.run(
        ["gh", "pr", "create", "--base", base, "--head", branch,
         "--title", f"fix: automated Ansible remediation ({len(rec.commits)} commits)",
         "--body", "\n".join(body_lines)],
        cwd=git_helper.repo_root(), capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else None


# ── Transcript writer ──────────────────────────────────────────────

class Transcript:
    """Append-only Markdown transcript writer used by the heal loop."""

    def __init__(self, path: Path, use_llm: bool):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lines: list[str] = []
        self._use_llm = use_llm
        self._header()

    def _header(self) -> None:
        self._lines.append("# Ansible-Heal-Agent — Demo Transcript")
        self._lines.append("")
        self._lines.append(f"- Started: `{time.strftime('%Y-%m-%d %H:%M:%S %Z')}`")
        # Report what the run can actually reach, not what was requested: with
        # --llm and no provider configured, "enabled: True" read as though a
        # model had been involved when every diagnosis came from the fallback.
        if not self._use_llm:
            self._lines.append(
                "- LLM bridge: `disabled` — every diagnosis below is deterministic")
        elif llm_bridge.is_available():
            self._lines.append(
                f"- LLM bridge: `enabled` — provider "
                f"`{llm_bridge.active_provider()}`, model "
                f"`{llm_bridge.active_model()}`")
        else:
            self._lines.append(
                "- LLM bridge: `requested, but no provider is reachable` — "
                "every diagnosis below came from the deterministic fallback")
        self._lines.append("")
        self._lines.append("---")
        self._lines.append("")

    def iteration_header(self, i: int, run) -> None:
        self._lines.append(f"## Iteration {i}")
        self._lines.append("")
        self._lines.append(f"- Pipeline run log: `{run.log_path}`")
        self._lines.append(f"- Exit code: `{run.exit_code}`")
        self._lines.append(f"- {len(run.failures)} failure(s) detected.")
        self._lines.append("")

    def failure_found(self, failure: dict) -> None:
        self._lines.append("### Failure detected")
        self._lines.append("```json")
        self._lines.append(json.dumps(failure, indent=2))
        self._lines.append("```")
        self._lines.append("")

    def diagnosis(self, failure: dict, diag: dict) -> None:
        # Label by what actually produced the diagnosis, not by whether the LLM
        # was *requested*: a run with --llm that fell back would otherwise be
        # recorded as an LLM diagnosis.
        used_llm = self._use_llm and "_fallback_reason" not in diag
        self._lines.append("**Diagnosis** (LLM)" if used_llm else "**Diagnosis** (fallback)")
        if "_fallback_reason" in diag:
            self._lines.append(f"_LLM unavailable, used fallback: `{diag['_fallback_reason']}`_")
        self._lines.append("```json")
        self._lines.append(json.dumps(diag, indent=2))
        self._lines.append("```")
        self._lines.append("")

    def patch_applied(self, fix: dict, patch: dict) -> None:
        self._lines.append("**Patch applied**")
        self._lines.append(f"- file: `{fix.get('target_file')}`")
        self._lines.append("")
        self._lines.append("```diff")
        self._lines.append(patch.get("diff", ""))
        self._lines.append("```")
        self._lines.append("")

    def patch_failed(self, fix: dict, err: str) -> None:
        self._lines.append("**Patch FAILED**")
        self._lines.append(f"- file: `{fix.get('target_file')}`")
        self._lines.append(f"- error: `{err}`")
        self._lines.append("")

    def patch_blocked(self, fix: dict, err: str) -> None:
        self._lines.append("**Patch BLOCKED by the write allowlist**")
        self._lines.append(f"- file: `{fix.get('target_file')}`")
        self._lines.append(f"- policy: `{err}`")
        self._lines.append("")

    def branch_created(self, branch: str, base: str) -> None:
        self._lines.append(f"### Branch `{branch}` created from `{base}`")
        self._lines.append("")
        self._lines.append("PR mode: the base branch will not be modified and the "
                           "pipeline will not be re-run.")
        self._lines.append("")

    def commit_failed(self, fix: dict, reason: str) -> None:
        self._lines.append(
            f"**Commit FAILED** — `{fix.get('target_file')}` was patched, but "
            f"{reason}. The change is on disk and staged, not committed.")
        self._lines.append("")

    def declined(self, failure: dict, reason: str) -> None:
        self._lines.append(f"**No fix proposed** — {reason}")
        self._lines.append("")

    def repeated_fix(self, signature: tuple) -> None:
        action, target, old, new = signature
        self._lines.append(
            f"_Already applied this iteration set: `{action}` on `{target}` "
            f"({old!r} -> {new!r}). Two failures are asking for the same edit, "
            f"so this does not count as progress._")
        self._lines.append("")

    def stalled(self, i: int, run) -> None:
        self._lines.append(f"### ⚠ Iteration {i} — no patch could be applied")
        self._lines.append("")
        self._lines.append("Re-running would produce an identical failure set, so "
                           "the loop stopped rather than exhausting its retries.")
        self._lines.append("")

    def dry_run_summary(self, result) -> None:
        self._lines.append("---")
        self._lines.append("")
        self._lines.append("## Dry run — nothing was written or committed")
        self._lines.append("")
        self._lines.append(f"- Proposals: `{len(result.proposals)}`")
        self._lines.append(f"- Blocked by allowlist: `{len(result.blocked)}`")
        self._lines.append("")
        for prop in result.proposals:
            self._lines.append(f"### Would edit `{prop.target_file}`")
            if prop.blocked_reason:
                self._lines.append(f"_BLOCKED: {prop.blocked_reason}_")
            self._lines.append("```diff")
            self._lines.append(prop.diff)
            self._lines.append("```")
            self._lines.append("")

    def pr_summary(self, result) -> None:
        self._lines.append("---")
        self._lines.append("")
        self._lines.append("## PR mode")
        self._lines.append("")
        self._lines.append(f"- Branch: `{result.branch}`")
        self._lines.append(f"- Pushed: `{result.pushed}`")
        if result.pr_url:
            self._lines.append(f"- Pull request: {result.pr_url}")
        if result.push_error:
            self._lines.append(f"- Push error: `{result.push_error}`")
        self._lines.append("")

    def committed(self, sha: str, fix: dict) -> None:
        self._lines.append(f"- committed: `{sha[:12]}` → `{fix.get('target_file')}`")
        self._lines.append("")

    @staticmethod
    def _read_log(run) -> str:
        """Return the run's log text.

        ``run.log_path`` is repo-relative; resolving it against the repo root
        is what makes this work. Without that these blocks rendered empty in
        every transcript the agent has ever written.
        """
        path = Path(run.log_path)
        if not path.is_absolute():
            path = repo_root() / path
        return path.read_text() if path.is_file() else "(run log not found)"

    def green(self, i: int, run) -> None:
        self._lines.append(f"### ✅ Iteration {i} → pipeline green")
        self._lines.append("")
        self._lines.append("```")
        self._lines.append(self._read_log(run))
        self._lines.append("```")
        self._lines.append("")

    def give_up(self, i: int, run) -> None:
        self._lines.append(f"### ❌ Iteration {i} — max retries exhausted, giving up")
        self._lines.append("")
        self._lines.append("```")
        self._lines.append(self._read_log(run))
        self._lines.append("```")
        self._lines.append("")

    def footer(self, result: HealResult) -> None:
        self._lines.append("---")
        self._lines.append("")
        self._lines.append("## Summary")
        self._lines.append("")
        self._lines.append(f"- **Success**: `{result.success}`")
        self._lines.append(f"- **Iterations**: `{result.iterations}`")
        self._lines.append(f"- **Final exit code**: `{result.final_exit_code}`")
        self._lines.append(f"- **Commits this session**: "
                           f"`{sum(len(r.commits) for r in result.history)}`")
        self._lines.append("")
        self._lines.append("### Recent git log")
        self._lines.append("```")
        self._lines.append(git_helper.log(20))
        self._lines.append("```")
        self._lines.append("")

    def save(self) -> str:
        self.path.write_text("\n".join(self._lines) + "\n")
        return str(self.path)
