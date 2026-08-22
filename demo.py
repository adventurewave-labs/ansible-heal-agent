#!/usr/bin/env python3
"""End-to-end demo entrypoint — ``python demo.py`` or ``make demo``.

By default the demo runs against a scratch workspace under ``.demo-workspace/``
rather than against this checkout. That matters: the agent's job is to *commit*
its fixes, and earlier versions of this demo committed them straight into this
repository — which is how dozens of "chore: reset to broken baseline" commits
ended up in its history before that history was squashed away. The scratch
workspace is a real git repo, so the demo still shows real commits; they just
land somewhere disposable.

Pass ``--in-place`` to run against this checkout instead (what CI does not do).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

# Ensure repo root is importable when run via `python demo.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import config  # noqa: E402
from agent import llm as llm_bridge  # noqa: E402
from agent.core import Transcript, heal  # noqa: E402
from pipeline import git_helper  # noqa: E402
from scenarios.seed import reset_to_baseline  # noqa: E402

CHECKOUT = Path(__file__).resolve().parent
WORKSPACE = CHECKOUT / ".demo-workspace"


def _prepare_workspace() -> Path:
    """Create a fresh, disposable git repo for the agent to operate on."""
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    WORKSPACE.mkdir(parents=True)
    return WORKSPACE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in-place", action="store_true",
                        help="Operate on this checkout instead of a scratch workspace "
                             "(this WILL add commits to your current branch).")
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args(argv)

    print("=" * 70)
    print(" ansible-heal-agent — end-to-end demo")
    print("=" * 70)
    print()

    target = CHECKOUT if args.in_place else _prepare_workspace()
    config.set_repo_root(target)

    where = "this checkout (--in-place)" if args.in_place else \
        f"scratch workspace {WORKSPACE.name}/"
    print(f"[1/4] Seeding the broken baseline into {where} ...")
    reset_to_baseline(commit=True)
    print(f"      3 failures seeded. HEAD = {git_helper.head_sha()[:12]}")
    print()

    use_llm = llm_bridge.is_available()
    if use_llm:
        print(f"[2/4] LLM bridge: ENABLED via {llm_bridge.active_provider()} "
              f"({llm_bridge.active_model()})")
    else:
        print("[2/4] LLM bridge: DISABLED (no provider configured) — using the "
              "deterministic fallback diagnoser.")
        print("      Set ANTHROPIC_API_KEY or OPENROUTER_API_KEY to exercise "
              "the LLM path.")
    print()

    ts = time.strftime("%Y%m%d-%H%M%S")
    transcript = Transcript(config.transcripts_dir() / f"demo-{ts}.md", use_llm=use_llm)

    print(f"[3/4] Running heal loop (max {args.max_retries} retries) ...")
    started = time.time()
    result = heal(
        playbook="ansible/playbooks/site.yml",
        max_retries=args.max_retries,
        use_llm=use_llm,
        transcript=transcript,
    )
    elapsed = time.time() - started
    print()
    print(f"      → success={result.success}  iterations={result.iterations}  "
          f"final_exit={result.final_exit_code}  elapsed={elapsed:.1f}s")
    print()

    print("      Commits the agent landed:")
    for line in git_helper.log(5).splitlines():
        print(f"        {line}")
    print()

    print("[4/4] Saving transcript ...")
    transcript.footer(result)
    path = transcript.save()
    shown = Path(path)
    if str(shown).startswith(str(CHECKOUT)):
        shown = shown.relative_to(CHECKOUT)
    print(f"      → {shown}")
    print()
    print("Done. Open the transcript to inspect the agent's reasoning.")
    return 0 if result.success else 2


if __name__ == "__main__":
    sys.exit(main())
