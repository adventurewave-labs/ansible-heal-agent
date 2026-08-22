"""LLM bridge — a provider-agnostic wrapper around a single chat completion.

This module is the ONLY place that talks to an LLM. The rest of the agent
operates on the structured output it returns. If no provider is configured or
the model returns malformed JSON, callers should fall back to the rule-based
diagnoser in ``agent.diagnoser.fallback_diagnose``.

Three backends are supported, selected with ``ANSIBLE_HEAL_LLM_PROVIDER``:

``anthropic``
    Anthropic Messages API. Needs ``ANTHROPIC_API_KEY``.
``openrouter``
    OpenRouter chat-completions API. Needs ``OPENROUTER_API_KEY``.
``z-ai``
    The proprietary ``z-ai`` CLI, if it happens to be on PATH.

When the env var is unset the provider is auto-detected in that order. HTTP is
done with the standard library only (``urllib.request``) so the agent has no
third-party runtime dependency for LLM access.

Secrets: API keys are read from the environment at call time and are never
logged, echoed, or embedded in an exception message — :class:`LLMError`
redacts them defensively on construction.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

PROVIDER_ENV = "ANSIBLE_HEAL_LLM_PROVIDER"
MODEL_ENV = "ANSIBLE_HEAL_LLM_MODEL"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

#: Provider preference order used when PROVIDER_ENV is unset.
PROVIDERS = ("anthropic", "openrouter", "z-ai")

#: Per-provider default model, overridable with MODEL_ENV.
PROVIDER_MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openrouter": "anthropic/claude-sonnet-4.5",
    "z-ai": "glm-4-plus",
}

#: Env var holding the credential for each HTTP provider.
PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

HTTP_TIMEOUT = 60
MAX_TOKENS = 4096
BACKOFF_SECONDS = 1.0

#: Model identifier reported in transcripts. Kept as a module attribute for
#: back-compat with the transcript writer; :func:`active_model` refreshes it.
DEFAULT_MODEL = PROVIDER_MODELS["anthropic"]

_REDACTED = "***REDACTED***"

# Where the z-ai CLI backend caches its JSON output between calls.
_TMP_DIR = Path(tempfile.gettempdir()) / "ansible-heal-agent"


# --------------------------------------------------------------------------
# Errors and secret redaction
# --------------------------------------------------------------------------


def _secret_values() -> list[str]:
    """Return credential-looking environment values that must never be shown."""
    secrets = []
    for name, value in os.environ.items():
        if not value or len(value) < 6:
            continue
        upper = name.upper()
        if (
            upper in PROVIDER_KEY_ENV.values()
            or "API_KEY" in upper
            or "TOKEN" in upper
            or "SECRET" in upper
        ):
            secrets.append(value)
    return secrets


def _redact(text: object) -> str:
    """Replace any known credential value inside ``text`` with a placeholder."""
    out = str(text)
    for secret in _secret_values():
        out = out.replace(secret, _REDACTED)
    return out


class LLMError(RuntimeError):
    """Raised for every LLM failure. Message content is always redacted."""

    def __init__(self, *args: object) -> None:
        super().__init__(*(_redact(a) for a in args))


class _Transient(Exception):
    """Internal: a provider failure worth retrying. Never escapes this module."""


# --------------------------------------------------------------------------
# Provider resolution
# --------------------------------------------------------------------------


def _provider_usable(name: str) -> bool:
    if name in PROVIDER_KEY_ENV:
        return bool(os.environ.get(PROVIDER_KEY_ENV[name], "").strip())
    if name == "z-ai":
        return shutil.which("z-ai") is not None
    return False


def _requirement(name: str) -> str:
    if name in PROVIDER_KEY_ENV:
        return f"{PROVIDER_KEY_ENV[name]} is not set"
    return "the 'z-ai' binary is not on PATH"


def _no_provider_message() -> str:
    return (
        "no LLM provider is available: set ANTHROPIC_API_KEY, set "
        "OPENROUTER_API_KEY, or install the 'z-ai' CLI on PATH. Select one "
        f"explicitly with {PROVIDER_ENV}=anthropic|openrouter|z-ai."
    )


def _resolve_provider() -> tuple[str | None, str]:
    """Return ``(provider_name, explanation)``; name is None when unusable."""
    pinned = os.environ.get(PROVIDER_ENV, "").strip().lower()
    if pinned:
        if pinned not in PROVIDERS:
            return None, (
                f"{PROVIDER_ENV} is set to an unknown provider "
                f"(expected one of: {', '.join(PROVIDERS)})"
            )
        if not _provider_usable(pinned):
            return None, (
                f"provider '{pinned}' was requested via {PROVIDER_ENV} but "
                f"{_requirement(pinned)}"
            )
        return pinned, f"selected explicitly via {PROVIDER_ENV}"

    for name in PROVIDERS:
        if _provider_usable(name):
            return name, "auto-detected"
    return None, _no_provider_message()


def active_provider() -> str | None:
    """Return the resolved provider name, or None if none is usable."""
    return _resolve_provider()[0]


def active_model() -> str:
    """Return the model id that would be used for the next call.

    Also refreshes the module-level :data:`DEFAULT_MODEL` so the transcript
    writer reports the model actually in play.
    """
    global DEFAULT_MODEL
    override = os.environ.get(MODEL_ENV, "").strip()
    if override:
        model = override
    else:
        provider = active_provider()
        model = PROVIDER_MODELS.get(provider or "", PROVIDER_MODELS["anthropic"])
    DEFAULT_MODEL = model
    return model


def is_available() -> bool:
    """Return True if any backend is usable right now."""
    return active_provider() is not None


# --------------------------------------------------------------------------
# HTTP helper (stdlib only)
# --------------------------------------------------------------------------


def _post_json(url: str, headers: dict, payload: dict, label: str) -> dict:
    """POST JSON and return the decoded JSON response.

    Raises :class:`_Transient` for anything retryable. Never includes request
    headers (which carry the API key) in an error message.
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001 - the error body is best-effort only
            detail = ""
        raise _Transient(f"{label} returned HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise _Transient(f"{label} network error: {exc.reason}") from None
    except TimeoutError:
        raise _Transient(f"{label} timed out after {HTTP_TIMEOUT}s") from None

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _Transient(f"{label} returned malformed JSON: {exc}") from None
    if not isinstance(decoded, dict):
        raise _Transient(f"{label} returned a non-object JSON payload")
    return decoded


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


def _chat_anthropic(prompt: str, system: str | None, model: str) -> str:
    key = os.environ.get(PROVIDER_KEY_ENV["anthropic"], "").strip()
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system

    data = _post_json(ANTHROPIC_URL, headers, payload, "anthropic")
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise _Transient("anthropic response had no 'content' list")
    parts = [
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type", "text") == "text"
    ]
    return "".join(parts)


def _chat_openrouter(prompt: str, system: str | None, model: str) -> str:
    key = os.environ.get(PROVIDER_KEY_ENV["openrouter"], "").strip()
    headers = {
        "Authorization": f"Bearer {key}",
        "content-type": "application/json",
    }
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": model, "max_tokens": MAX_TOKENS, "messages": messages}

    data = _post_json(OPENROUTER_URL, headers, payload, "openrouter")
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise _Transient(f"unexpected openrouter response shape: {exc}") from None
    return content or ""


def _chat_zai(prompt: str, system: str | None, model: str) -> str:
    try:
        _TMP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _Transient(f"could not create the z-ai scratch dir: {exc}") from None

    out_file = _TMP_DIR / f"resp-{int(time.time() * 1000)}.json"
    cmd = ["z-ai", "chat", "--prompt", prompt, "--output", str(out_file)]
    if system:
        cmd += ["--system", system]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=HTTP_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise _Transient(f"z-ai CLI timed out after {HTTP_TIMEOUT}s") from None
    except OSError as exc:
        raise _Transient(f"could not execute the z-ai CLI: {exc}") from None

    if proc.returncode != 0:
        stderr = (proc.stderr or "")[:400]
        raise _Transient(f"z-ai exit={proc.returncode} stderr={stderr}")

    try:
        payload = json.loads(out_file.read_text())
        return payload["choices"][0]["message"]["content"] or ""
    except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise _Transient(f"z-ai returned malformed JSON: {exc}") from None


_BACKENDS: dict[str, Callable[[str, str | None, str], str]] = {
    "anthropic": _chat_anthropic,
    "openrouter": _chat_openrouter,
    "z-ai": _chat_zai,
}


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def chat(prompt: str, system: str | None = None, max_retries: int = 2) -> str:
    """Send a single chat completion request and return the assistant's text.

    ``max_retries`` is the total number of attempts (2 by default), with a
    linear backoff between them. Raises :class:`LLMError` — and only
    :class:`LLMError` — when every attempt fails.
    """
    provider, why = _resolve_provider()
    if provider is None:
        raise LLMError(why)

    model = active_model()
    backend = _BACKENDS[provider]
    attempts = max(1, int(max_retries))
    last_err: str | None = None

    for attempt in range(1, attempts + 1):
        try:
            content = backend(prompt, system, model)
            if content and content.strip():
                return content.strip()
            last_err = f"{provider} returned an empty completion"
        except _Transient as exc:
            last_err = str(exc)
        except Exception as exc:  # noqa: BLE001 - nothing escapes as non-LLMError
            last_err = f"{provider} backend raised {type(exc).__name__}: {exc}"
        if attempt < attempts:
            time.sleep(BACKOFF_SECONDS * attempt)

    raise LLMError(
        f"{provider} request failed after {attempts} attempt(s) "
        f"using model '{model}': {last_err}"
    )


def chat_json(prompt: str, system: str | None = None) -> dict[str, Any]:
    """Like :func:`chat`, but parse the response as a JSON object.

    The LLM is prompted to return ONLY a JSON object. If the response contains
    surrounding markdown fences or commentary, we extract the first {...} block.
    """
    raw = chat(prompt, system=system)
    # Strip ```json fences if present
    if "```" in raw:
        lines = raw.splitlines()
        start = next(
            (i for i, line in enumerate(lines) if line.strip().startswith("```")), 0
        )
        end = next(
            (
                i
                for i, line in enumerate(lines)
                if i > start and line.strip().startswith("```")
            ),
            len(lines),
        )
        raw = "\n".join(lines[start + 1 : end])
    # Find the first {...}...{...} block
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMError(f"LLM response contained no JSON object: {raw[:200]}")
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM response was not valid JSON: {exc}") from None
