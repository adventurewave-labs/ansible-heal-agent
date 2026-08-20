"""The agent's heal loop.

Loop contract:
  iter 0: run pipeline → if green, exit. else collect failures.
  iter 1..max_retries:
    for each failure: diagnose → patch → commit.
    re-run pipeline. if green, exit.
  else: declare defeat and write a final transcript.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agent import llm as llm_bridge
from agent import log_scanner, diagnoser, patcher, committer, pipeline_restarter
from pipeline import git_helper


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
class HealResult:
    success: bool
    iterations: int
    final_exit_code: int
    history: list[IterationRecord] = field(default_factory=list)
    transcript_path: Optional[str] = None


def heal(playbook: str = "ansible/playbooks/site.yml",
         max_retries: int = 3,
         use_llm: bool = True,
         transcript: Optional["Transcript"] = None) -> HealResult:
    """Run the heal loop. Returns a HealResult summarising the run."""
    git_helper.init_if_needed()
    history: list[IterationRecord] = []
    result = HealResult(success=False, iterations=0, final_exit_code=-1, history=history)

    for i in range(max_retries + 1):
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

        if run.exit_code == 0:
            result.success = True
            result.iterations = i
            result.final_exit_code = 0
            history.append(rec)
            if transcript:
                transcript.green(i, run)
            break

        if i == max_retries:
            # Last iteration and still failing — give up.
            result.success = False
            result.iterations = i
            result.final_exit_code = run.exit_code
            history.append(rec)
            if transcript:
                transcript.give_up(i, run)
            break

        # Diagnose + patch + commit each failure.
        for failure in run.failures:
            if transcript:
                transcript.failure_found(failure)

            diag = diagnoser.diagnose(failure, use_llm=use_llm)
            rec.diagnoses.append(diag)
            if transcript:
                transcript.diagnosis(failure, diag)

            fix = diag.get("fix", {})
            try:
                patch = patcher.apply_fix(fix)
                rec.patches.append(patch)
                if transcript:
                    transcript.patch_applied(fix, patch)
            except patcher.PatchError as e:
                if transcript:
                    transcript.patch_failed(fix, str(e))
                # If the LLM produced a malformed patch, retry once with the
                # deterministic fallback — it produces patches that are
                # guaranteed to match the baseline.
                if use_llm and "_fallback_reason" not in diag:
                    fb_diag = diagnoser.fallback_diagnose(failure)
                    fb_diag["_fallback_reason"] = (
                        f"LLM patch failed validation: {e}"
                    )
                    rec.diagnoses.append(fb_diag)
                    if transcript:
                        transcript.diagnosis(failure, fb_diag)
                    fb_fix = fb_diag.get("fix", {})
                    try:
                        patch = patcher.apply_fix(fb_fix)
                        rec.patches.append(patch)
                        if transcript:
                            transcript.patch_applied(fb_fix, patch)
                        fix = fb_fix
                        diag = fb_diag
                    except patcher.PatchError as e2:
                        if transcript:
                            transcript.patch_failed(fb_fix, str(e2))
                        continue
                else:
                    continue

            sha = committer.commit_fix(fix, diag)
            rec.commits.append(sha)
            if transcript:
                transcript.committed(sha, fix)

        history.append(rec)

    return result


# ── Transcript writer ──────────────────────────────────────────────────────────

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
        self._lines.append(f"- LLM bridge enabled: `{self._use_llm}`")
        if self._use_llm:
            self._lines.append(f"- LLM model: `{llm_bridge.DEFAULT_MODEL}` "
                               f"(available: `{llm_bridge.is_available()}`)")
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
        self._lines.append("**Diagnosis** (LLM" if self._use_llm else "**Diagnosis** (fallback)")
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

    def committed(self, sha: str, fix: dict) -> None:
        self._lines.append(f"- committed: `{sha[:12]}` → `{fix.get('target_file')}`")
        self._lines.append("")

    def green(self, i: int, run) -> None:
        self._lines.append(f"### ✅ Iteration {i} → pipeline green")
        self._lines.append("")
        self._lines.append("```")
        self._lines.append(Path(run.log_path).read_text() if Path(run.log_path).exists() else "")
        self._lines.append("```")
        self._lines.append("")

    def give_up(self, i: int, run) -> None:
        self._lines.append(f"### ❌ Iteration {i} — max retries exhausted, giving up")
        self._lines.append("")
        self._lines.append("```")
        self._lines.append(Path(run.log_path).read_text() if Path(run.log_path).exists() else "")
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
