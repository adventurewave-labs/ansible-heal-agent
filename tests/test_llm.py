"""Tests for the provider-agnostic LLM bridge.

Every test is hermetic: the autouse ``clean_env`` fixture clears the provider,
model and credential environment variables, pretends the ``z-ai`` binary is
absent, and stubs ``time.sleep`` so retry backoff costs nothing. No test makes
a network call or needs a real API key.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from agent import llm

ENV_VARS = (
    "ANSIBLE_HEAL_LLM_PROVIDER",
    "ANSIBLE_HEAL_LLM_MODEL",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Neutralise the developer's environment for every test."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    yield


def use_zai_binary(monkeypatch):
    """Pretend the z-ai CLI is installed."""
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/z-ai" if name == "z-ai" else None
    )


class FakeResponse:
    """Minimal stand-in for the object returned by urlopen()."""

    def __init__(self, payload):
        if isinstance(payload, (bytes, str)):
            self._body = payload if isinstance(payload, bytes) else payload.encode()
        else:
            self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def fake_urlopen(monkeypatch, *responses):
    """Install a fake urlopen returning/raising ``responses`` in order.

    Returns the list of recorded requests (url, headers, decoded JSON body).
    """
    queue = list(responses)
    calls = []

    def _urlopen(request, timeout=None):
        calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "timeout": timeout,
                "headers": {k.lower(): v for k, v in request.header_items()},
                "body": json.loads(request.data.decode("utf-8")),
            }
        )
        item = queue.pop(0) if queue else responses[-1]
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return calls


def http_error(code=500, body=b"upstream exploded"):
    return urllib.error.HTTPError(
        "https://example.invalid", code, "Server Error", {}, io.BytesIO(body)
    )


def anthropic_payload(text):
    return {"content": [{"type": "text", "text": text}]}


def openrouter_payload(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


# ---------------------------------------------------------------------------
# Provider auto-detection precedence
# ---------------------------------------------------------------------------


def test_autodetect_prefers_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bbbbbbbbbbbb")
    use_zai_binary(monkeypatch)

    assert llm.active_provider() == "anthropic"
    assert llm.is_available() is True
    assert llm.active_model() == "claude-sonnet-4-5"


def test_autodetect_falls_back_to_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bbbbbbbbbbbb")
    use_zai_binary(monkeypatch)

    assert llm.active_provider() == "openrouter"
    assert llm.is_available() is True
    assert llm.active_model() == llm.PROVIDER_MODELS["openrouter"]


def test_autodetect_falls_back_to_zai_binary(monkeypatch):
    use_zai_binary(monkeypatch)

    assert llm.active_provider() == "z-ai"
    assert llm.is_available() is True
    assert llm.active_model() == "glm-4-plus"


def test_autodetect_none_available():
    assert llm.active_provider() is None
    assert llm.is_available() is False


def test_explicit_provider_overrides_precedence(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bbbbbbbbbbbb")
    monkeypatch.setenv("ANSIBLE_HEAL_LLM_PROVIDER", "OpenRouter")

    assert llm.active_provider() == "openrouter"


def test_explicit_provider_without_credential_is_unavailable(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bbbbbbbbbbbb")
    monkeypatch.setenv("ANSIBLE_HEAL_LLM_PROVIDER", "anthropic")

    assert llm.active_provider() is None
    assert llm.is_available() is False

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("hi")
    assert "ANTHROPIC_API_KEY is not set" in str(excinfo.value)


def test_explicit_zai_without_binary_is_unavailable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    monkeypatch.setenv("ANSIBLE_HEAL_LLM_PROVIDER", "z-ai")

    assert llm.active_provider() is None
    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("hi")
    assert "'z-ai' binary is not on PATH" in str(excinfo.value)


def test_unknown_explicit_provider_is_reported(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    monkeypatch.setenv("ANSIBLE_HEAL_LLM_PROVIDER", "not-a-provider")

    assert llm.active_provider() is None
    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("hi")
    assert "unknown provider" in str(excinfo.value)


def test_chat_without_any_provider_raises_llm_error():
    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("hello")
    assert "no LLM provider is available" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Model resolution / DEFAULT_MODEL back-compat
# ---------------------------------------------------------------------------


def test_model_env_var_overrides_provider_default(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    monkeypatch.setenv("ANSIBLE_HEAL_LLM_MODEL", "claude-haiku-9")

    assert llm.active_model() == "claude-haiku-9"


def test_default_model_attribute_tracks_resolved_model(monkeypatch):
    assert isinstance(llm.DEFAULT_MODEL, str)
    use_zai_binary(monkeypatch)
    llm.active_model()
    assert llm.DEFAULT_MODEL == "glm-4-plus"


def test_active_model_without_provider_falls_back_to_anthropic_default():
    assert llm.active_model() == llm.PROVIDER_MODELS["anthropic"]


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------


def test_anthropic_happy_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-happy-path-key")
    calls = fake_urlopen(monkeypatch, anthropic_payload("  all good  "))

    assert llm.chat("why did it fail?", system="be terse") == "all good"

    (call,) = calls
    assert call["url"] == llm.ANTHROPIC_URL
    assert call["method"] == "POST"
    assert call["timeout"] == 60
    assert call["headers"]["x-api-key"] == "sk-ant-happy-path-key"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["body"]["model"] == "claude-sonnet-4-5"
    assert call["body"]["max_tokens"] == llm.MAX_TOKENS
    assert call["body"]["system"] == "be terse"
    assert call["body"]["messages"] == [
        {"role": "user", "content": "why did it fail?"}
    ]


def test_anthropic_concatenates_text_blocks_and_omits_absent_system(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    payload = {
        "content": [
            {"type": "text", "text": "one "},
            {"type": "thinking", "thinking": "ignored"},
            {"type": "text", "text": "two"},
        ]
    }
    calls = fake_urlopen(monkeypatch, payload)

    assert llm.chat("p") == "one two"
    assert "system" not in calls[0]["body"]


def test_anthropic_bad_response_shape_raises_llm_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    fake_urlopen(monkeypatch, {"error": "nope"})

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p")
    assert "content" in str(excinfo.value)


def test_non_json_http_body_raises_llm_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    fake_urlopen(monkeypatch, b"<html>gateway</html>")

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p")
    assert "malformed JSON" in str(excinfo.value)


def test_json_array_response_raises_llm_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    fake_urlopen(monkeypatch, b"[1, 2, 3]")

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p")
    assert "non-object JSON" in str(excinfo.value)


def test_empty_completion_raises_llm_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    fake_urlopen(monkeypatch, anthropic_payload("   "))

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p")
    assert "empty completion" in str(excinfo.value)


# ---------------------------------------------------------------------------
# OpenRouter backend
# ---------------------------------------------------------------------------


def test_openrouter_happy_path(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-happy-path-key")
    calls = fake_urlopen(monkeypatch, openrouter_payload("routed answer"))

    assert llm.chat("question", system="sys prompt") == "routed answer"

    (call,) = calls
    assert call["url"] == llm.OPENROUTER_URL
    assert call["headers"]["authorization"] == "Bearer sk-or-happy-path-key"
    assert call["body"]["model"] == llm.PROVIDER_MODELS["openrouter"]
    assert call["body"]["messages"] == [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "question"},
    ]


def test_openrouter_without_system_sends_only_user_message(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bbbbbbbbbbbb")
    calls = fake_urlopen(monkeypatch, openrouter_payload("ok"))

    assert llm.chat("question") == "ok"
    assert calls[0]["body"]["messages"] == [{"role": "user", "content": "question"}]


def test_openrouter_bad_response_shape_raises_llm_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bbbbbbbbbbbb")
    fake_urlopen(monkeypatch, {"choices": []})

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p")
    assert "unexpected openrouter response shape" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Retry semantics
# ---------------------------------------------------------------------------


def test_retry_then_succeed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    slept = []
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))
    calls = fake_urlopen(
        monkeypatch,
        urllib.error.URLError("connection reset"),
        anthropic_payload("second attempt wins"),
    )

    assert llm.chat("p") == "second attempt wins"
    assert len(calls) == 2
    assert slept == [llm.BACKOFF_SECONDS]


def test_all_retries_exhausted_raises_llm_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    calls = fake_urlopen(monkeypatch, http_error(503, b"overloaded"), http_error(503))

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p", max_retries=2)

    message = str(excinfo.value)
    assert len(calls) == 2
    assert "failed after 2 attempt(s)" in message
    assert "HTTP 503" in message
    assert "claude-sonnet-4-5" in message


def test_max_retries_controls_attempt_count(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    calls = fake_urlopen(monkeypatch, *[http_error(500)] * 5)

    with pytest.raises(llm.LLMError):
        llm.chat("p", max_retries=4)
    assert len(calls) == 4

    calls.clear()
    with pytest.raises(llm.LLMError):
        llm.chat("p", max_retries=0)
    assert len(calls) == 1


def test_timeout_error_is_wrapped(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    fake_urlopen(monkeypatch, TimeoutError("too slow"))

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p", max_retries=1)
    assert "timed out after 60s" in str(excinfo.value)


def test_unexpected_backend_exception_becomes_llm_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")

    def boom(request, timeout=None):
        raise ZeroDivisionError("something very unexpected")

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p", max_retries=1)
    assert "ZeroDivisionError" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


def test_api_key_never_appears_in_raised_error(monkeypatch):
    secret = "sk-ant-SUPER-SECRET-VALUE-9876543210"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    # Worst case: the provider echoes the key straight back in its error body.
    fake_urlopen(
        monkeypatch,
        http_error(401, f"invalid key: {secret}".encode()),
        http_error(401, f"invalid key: {secret}".encode()),
    )

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p")

    rendered = f"{excinfo.value!r} {excinfo.value!s} {excinfo.value.args}"
    assert secret not in rendered
    assert "***REDACTED***" in rendered


def test_openrouter_key_redacted_too(monkeypatch):
    secret = "sk-or-ANOTHER-SECRET-1234567890"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    fake_urlopen(monkeypatch, http_error(429, f"rate limit for {secret}".encode()))

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p", max_retries=1)
    assert secret not in str(excinfo.value)


# ---------------------------------------------------------------------------
# chat_json()
# ---------------------------------------------------------------------------


def test_chat_json_strips_markdown_fences(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    fenced = '```json\n{"diagnosis": "stale host", "confidence": 0.9}\n```'
    fake_urlopen(monkeypatch, anthropic_payload(fenced))

    assert llm.chat_json("p") == {"diagnosis": "stale host", "confidence": 0.9}


def test_chat_json_extracts_object_surrounded_by_prose(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    chatty = (
        "Sure! Here is what I found.\n"
        '{"diagnosis": "inventory drift", "patch": {"file": "hosts.ini"}}\n'
        "Let me know if you want more detail."
    )
    fake_urlopen(monkeypatch, anthropic_payload(chatty))

    assert llm.chat_json("p", system="s") == {
        "diagnosis": "inventory drift",
        "patch": {"file": "hosts.ini"},
    }


def test_chat_json_without_any_object_raises(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    fake_urlopen(monkeypatch, anthropic_payload("I could not determine a cause."))

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat_json("p")
    assert "no JSON object" in str(excinfo.value)


def test_chat_json_with_broken_json_raises_llm_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    fake_urlopen(monkeypatch, anthropic_payload('{"diagnosis": "oops",}'))

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat_json("p")
    assert "not valid JSON" in str(excinfo.value)


def test_chat_json_extracts_the_outermost_brace_span(monkeypatch):
    """Extraction spans the first '{' to the last '}', as it always has."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    fake_urlopen(
        monkeypatch, anthropic_payload('prefix ["a", {"b": {"c": 1}}] suffix')
    )

    assert llm.chat_json("p") == {"b": {"c": 1}}


# ---------------------------------------------------------------------------
# z-ai CLI backend
# ---------------------------------------------------------------------------


def _fake_run(monkeypatch, *, returncode=0, stderr="", written=None, raises=None):
    recorded = {}

    def run(cmd, capture_output=False, text=False, timeout=None):
        recorded["cmd"] = cmd
        recorded["timeout"] = timeout
        if raises is not None:
            raise raises
        if written is not None:
            out_path = cmd[cmd.index("--output") + 1]
            with open(out_path, "w", encoding="utf-8") as handle:
                handle.write(written)
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return recorded


def test_zai_happy_path(monkeypatch):
    use_zai_binary(monkeypatch)
    body = json.dumps({"choices": [{"message": {"content": " from the CLI "}}]})
    recorded = _fake_run(monkeypatch, written=body)

    assert llm.chat("p", system="s") == "from the CLI"
    assert recorded["cmd"][:3] == ["z-ai", "chat", "--prompt"]
    assert "--system" in recorded["cmd"]
    assert recorded["timeout"] == 60


def test_zai_nonzero_exit_raises_llm_error(monkeypatch):
    use_zai_binary(monkeypatch)
    _fake_run(monkeypatch, returncode=3, stderr="boom")

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p", max_retries=1)
    assert "z-ai exit=3" in str(excinfo.value)


def test_zai_malformed_output_raises_llm_error(monkeypatch):
    use_zai_binary(monkeypatch)
    _fake_run(monkeypatch, written="not json at all")

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p", max_retries=1)
    assert "malformed JSON" in str(excinfo.value)


def test_zai_timeout_raises_llm_error(monkeypatch):
    use_zai_binary(monkeypatch)
    _fake_run(monkeypatch, raises=subprocess.TimeoutExpired(cmd="z-ai", timeout=60))

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p", max_retries=1)
    assert "timed out" in str(excinfo.value)


def test_zai_missing_binary_at_exec_time_raises_llm_error(monkeypatch):
    use_zai_binary(monkeypatch)
    _fake_run(monkeypatch, raises=OSError("No such file or directory"))

    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat("p", max_retries=1)
    assert "could not execute" in str(excinfo.value)
