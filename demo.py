#!/usr/bin/env python3
"""End-to-end demo entrypoint — `python demo.py` or `make demo`.

Runs the full heal loop against the broken baseline and writes a Markdown
transcript under transcripts/.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Ensure repo root is importable when run via `python demo.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenarios.seed import reset_to_baseline            # noqa: E402
from agent.core import heal, Transcript                 # noqa: E402
from agent import llm as llm_bridge                     # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent


def main() -> int:
    print("=" * 70)
    print(" ansible-heal-agent — end-to-end demo")
    print("=" * 70)
    print()

    # 1. Reset to broken baseline
    print("[1/4] Resetting repo to broken baseline ...")
    reset_to_baseline()
    print("      done.")
    print()

    # 2. Set up transcript
    ts = time.strftime("%Y%m%d-%H%M%S")
    transcript_path = REPO_ROOT / "transcripts" / f"demo-{ts}.md"
    use_llm = llm_bridge.is_available()
    transcript = Transcript(transcript_path, use_llm=use_llm)
    print(f"[2/4] LLM bridge: {'ENABLED (z-ai CLI)' if use_llm else 'DISABLED — using fallback diagnoser'}")
    print()

    # 3. Run the heal loop
    print("[3/4] Running heal loop (max 3 retries) ...")
    result = heal(
        playbook="ansible/playbooks/site.yml",
        max_retries=3,
        use_llm=use_llm,
        transcript=transcript,
    )
    print()
    print(f"      → success={result.success}  iterations={result.iterations}  "
          f"final_exit={result.final_exit_code}")
    print()

    # 4. Save transcript
    print("[4/4] Saving transcript ...")
    transcript.footer(result)
    path = transcript.save()
    print(f"      → {path}")
    print()
    print("Done. Open the transcript to inspect the agent's reasoning.")
    return 0 if result.success else 2


if __name__ == "__main__":
    sys.exit(main())
