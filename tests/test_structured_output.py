"""Structured-output path of the LLM bridge and the diagnosis contract.

Hermetic: every HTTP call goes through the fake urlopen from test_llm.
"""

from __future__ import annotations

import json

import pytest

from agent import diagnoser, llm
from tests.test_llm import clean_env, fake_urlopen, http_error  # noqa: F401

GOOD = {
    "diagnosis": "inventory host renamed",
    "failure_type": "no_hosts_matched",
    "fix": {
        "action": "edit_file",
        "target_file": "ansible/inventory.yml",
        "search": "web-1",
        "replace": "web-01",
        "rationale": "match the play",
    },
}


def tool_use_payload(obj, name="submit_diagnosis"):
    return {"content": [
        {"type": "text", "text": "calling tool"},
        {"type": "tool_use", "id": "tu_1", "name": name, "input": obj},
    ]}


def test_anthropic_forces_the_schema_tool_and_caches_system(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    calls = fake_urlopen(monkeypatch, tool_use_payload(GOOD))

    out = llm.chat_json("p", system="sys", schema=diagnoser.DIAGNOSIS_SCHEMA)

    assert out == GOOD
    body = calls[0]["body"]
    assert body["tool_choice"] == {"type": "tool", "name": "submit_diagnosis"}
    assert body["tools"][0]["input_schema"] == diagnoser.DIAGNOSIS_SCHEMA["schema"]
    assert body["system"] == [{
        "type": "text", "text": "sys", "cache_control": {"type": "ephemeral"},
    }]


def test_anthropic_without_the_tool_block_is_an_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    fake_urlopen(monkeypatch, {"content": [{"type": "text", "text": "{}"}]})
    with pytest.raises(llm.LLMError, match="tool_use"):
        llm.chat_json("p", schema=diagnoser.DIAGNOSIS_SCHEMA)


def test_openrouter_sends_strict_json_schema(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bbbbbbbbbbbb")
    calls = fake_urlopen(monkeypatch, {"choices": [
        {"message": {"content": json.dumps(GOOD)}}]})

    assert llm.chat_json("p", schema=diagnoser.DIAGNOSIS_SCHEMA) == GOOD
    fmt = calls[0]["body"]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d.pop("fix"), "missing required key 'fix'"),
    (lambda d: d.update(failure_type="guess"), "not one of"),
    (lambda d: d["fix"].update(action="run_shell"), "not one of"),
    (lambda d: d["fix"].update(search=""), "shorter than"),
    (lambda d: d["fix"].update(extra="x"), "unexpected key 'extra'"),
    (lambda d: d.update(diagnosis=7), "expected string"),
])
def test_schema_violations_are_rejected(monkeypatch, mutate, needle):
    bad = json.loads(json.dumps(GOOD))
    mutate(bad)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bbbbbbbbbbbb")
    fake_urlopen(monkeypatch, {"choices": [{"message": {"content": json.dumps(bad)}}]})
    with pytest.raises(llm.LLMError, match=needle):
        llm.chat_json("p", schema=diagnoser.DIAGNOSIS_SCHEMA)


def test_client_errors_are_not_retried(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    calls = fake_urlopen(monkeypatch, http_error(400, b"bad"), http_error(400, b"bad"))
    with pytest.raises(llm.LLMError, match="not retried"):
        llm.chat("p", max_retries=3)
    assert len(calls) == 1


def test_rate_limits_are_retried(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    calls = fake_urlopen(
        monkeypatch, http_error(429, b"slow down"),
        {"content": [{"type": "text", "text": "ok"}]},
    )
    assert llm.chat("p", max_retries=2) == "ok"
    assert len(calls) == 2


def test_schema_violation_falls_back_to_deterministic_rules(monkeypatch):
    def bad(_failure):
        raise llm.LLMError("LLM response violated the schema: $.fix: missing")
    monkeypatch.setattr(diagnoser, "llm_diagnose", bad)
    monkeypatch.setattr(diagnoser, "fallback_diagnose", lambda f: {"fix": {}})
    out = diagnoser.diagnose({"type": "other"}, use_llm=True)
    assert "violated the schema" in out["_fallback_reason"]
