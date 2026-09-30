"""OpenTelemetry instrumentation: spans are emitted when OTel is present,
nothing breaks when it is absent, and no content leaks into attributes."""

from __future__ import annotations

import shutil

import pytest

from agent import llm, telemetry
from tests.test_llm import clean_env, fake_urlopen  # noqa: F401

_REAL_WHICH = shutil.which  # clean_env stubs it; the heal loop needs ansible-core

sdk = pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

_EXPORTER = InMemorySpanExporter()
_PROVIDER = sdk.TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))


@pytest.fixture
def spans(monkeypatch):
    # get_tracer is looked up per span, so patching the module-level accessor
    # is enough; the global provider is never mutated across tests.
    monkeypatch.setattr(
        telemetry._trace, "get_tracer",
        lambda name, *a, **k: _PROVIDER.get_tracer(name),
    )
    monkeypatch.delenv(telemetry.DISABLE_ENV, raising=False)
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


def test_llm_call_emits_genai_semconv_span(monkeypatch, spans):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    fake_urlopen(monkeypatch, {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 120, "output_tokens": 7,
                  "cache_read_input_tokens": 100},
    })
    assert llm.chat("the prompt text", system="sys") == "ok"

    (s,) = spans.get_finished_spans()
    model = llm.PROVIDER_MODELS["anthropic"]
    assert s.name == f"chat {model}"
    a = s.attributes
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.provider.name"] == "anthropic"
    assert a["gen_ai.request.model"] == model
    assert a["gen_ai.usage.input_tokens"] == 120
    assert a["gen_ai.usage.output_tokens"] == 7
    assert a["gen_ai.usage.cache_read.input_tokens"] == 100
    assert a["ansible_heal.llm.attempts"] == 1
    # Never the prompt, the completion, or the key.
    rendered = repr(dict(a))
    assert "the prompt text" not in rendered
    assert "sk-ant" not in rendered


def test_llm_failure_marks_span_as_error(monkeypatch, spans):
    from tests.test_llm import http_error
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-aaaaaaaaaaaa")
    fake_urlopen(monkeypatch, http_error(401, b"bad key"))
    with pytest.raises(llm.LLMError):
        llm.chat("p")
    (s,) = spans.get_finished_spans()
    assert s.status.status_code == trace.StatusCode.ERROR
    assert any(e.name == "exception" for e in s.events)


def test_heal_run_emits_component_spans(scratch_repo, spans, monkeypatch):
    from agent import core
    monkeypatch.setattr(shutil, "which", _REAL_WHICH)
    if not all(_REAL_WHICH(b) for b in ("ansible", "ansible-inventory", "ansible-doc")):
        pytest.skip("apply mode requires ansible-core on PATH")
    result = core.heal(use_llm=False, mode=core.MODE_APPLY)
    names = [s.name for s in spans.get_finished_spans()]
    assert names[-1] == "ansible_heal.run"
    assert "ansible_heal.pipeline.run" in names
    assert "ansible_heal.diagnose" in names
    assert "ansible_heal.commit" in names
    run = spans.get_finished_spans()[-1]
    assert run.attributes["ansible_heal.success"] == result.success
    assert run.attributes["ansible_heal.mode"] == "apply"
    # Children share the run's trace.
    assert {s.context.trace_id for s in spans.get_finished_spans()} == {
        run.context.trace_id}


def test_disable_env_turns_spans_off(monkeypatch, spans):
    monkeypatch.setenv(telemetry.DISABLE_ENV, "0")
    with telemetry.span("x"):
        pass
    assert spans.get_finished_spans() == ()


def test_noop_when_opentelemetry_is_absent(monkeypatch):
    monkeypatch.setattr(telemetry, "_trace", None)
    assert telemetry.enabled() is False
    with telemetry.span("x", a=1) as s:
        telemetry.set_attrs(s, b=2)
        telemetry.mark_error(s, "nope")

    @telemetry.traced("y", attrs=lambda: 1 / 0)  # extractor bugs never surface
    def f():
        return 42
    assert f() == 42


def test_broken_attribute_extractor_never_breaks_the_call(spans):
    @telemetry.traced("z", attrs=lambda: 1 / 0, result_attrs=lambda r: 1 / 0)
    def f():
        return "fine"
    assert f() == "fine"
    (s,) = spans.get_finished_spans()
    assert s.name == "z"


def test_configure_from_env_is_inert_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert telemetry.configure_from_env() is False
