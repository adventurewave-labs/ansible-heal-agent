"""LLM bridge — wraps the z-ai CLI (z-ai-web-dev-sdk).

This module is the ONLY place that talks to the LLM. The rest of the agent
operates on the structured output it returns. If the LLM is unavailable or
returns malformed JSON, callers should fall back to the rule-based diagnoser
in agent.diagnoser.fallback_diagnose.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

# Model identifier reported in transcripts. The z-ai CLI defaults to GLM-4-Plus.
DEFAULT_MODEL = "glm-4-plus"

# Where to cache the chat JSON output between calls.
_TMP_DIR = Path(tempfile.gettempdir()) / "ansible-heal-agent"
_TMP_DIR.mkdir(parents=True, exist_ok=True)


class LLMError(RuntimeError):
    pass


def is_available() -> bool:
    """Return True if the z-ai CLI is on PATH."""
    return shutil.which("z-ai") is not None


def chat(prompt: str, system: Optional[str] = None, max_retries: int = 2) -> str:
    """Send a single chat completion request and return the assistant's text.

    Retries once on transient failure (network, malformed JSON). Raises LLMError
    if all retries are exhausted.
    """
    if not is_available():
        raise LLMError("z-ai CLI not found on PATH. Install z-ai-web-dev-sdk "
                        "or set PIPELINE_RUNNER=mock to use the rule-based fallback.")

    # Write the prompt to a file to avoid shell-escaping pitfalls on long prompts.
    prompt_file = _TMP_DIR / f"prompt-{int(time.time()*1000)}.txt"
    prompt_file.write_text(prompt)

    out_file = _TMP_DIR / f"resp-{int(time.time()*1000)}.json"

    cmd = ["z-ai", "chat", "--prompt", str(prompt_file), "--output", str(out_file)]
    if system:
        sys_file = _TMP_DIR / f"sys-{int(time.time()*1000)}.txt"
        sys_file.write_text(system)
        cmd += ["--system", str(sys_file)]

    last_err: Optional[str] = None
    for attempt in range(1, max_retries + 1):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                last_err = f"z-ai exit={proc.returncode} stderr={proc.stderr[:400]}"
                time.sleep(1 * attempt)
                continue
            payload = json.loads(out_file.read_text())
            content = payload["choices"][0]["message"]["content"]
            if not content:
                raise LLMError("z-ai returned an empty completion")
            return content.strip()
        except subprocess.TimeoutExpired:
            last_err = "z-ai CLI timed out after 60s"
            time.sleep(1 * attempt)
        except (json.JSONDecodeError, KeyError) as e:
            last_err = f"z-ai returned malformed JSON: {e}"
            time.sleep(1 * attempt)

    raise LLMError(last_err or "z-ai CLI failed for unknown reasons")


def chat_json(prompt: str, system: Optional[str] = None) -> dict[str, Any]:
    """Like chat(), but parse the response as a JSON object.

    The LLM is prompted to return ONLY a JSON object. If the response contains
    surrounding markdown fences or commentary, we extract the first {...} block.
    """
    raw = chat(prompt, system=system)
    # Strip ```json fences if present
    if "```" in raw:
        lines = raw.splitlines()
        start = next((i for i, l in enumerate(lines) if l.strip().startswith("```")), 0)
        end = next((i for i, l in enumerate(lines) if i > start and l.strip().startswith("```")), len(lines))
        raw = "\n".join(lines[start + 1:end])
    # Find the first {...}...{...} block
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMError(f"LLM response contained no JSON object: {raw[:200]}")
    return json.loads(raw[start:end + 1])
