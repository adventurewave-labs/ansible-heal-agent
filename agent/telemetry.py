"""OpenTelemetry instrumentation (PRD NFR-5).

Every component of the heal loop emits a span: the run, each diagnosis, each
LLM call, each commit and each pipeline run. LLM calls follow the OpenTelemetry
GenAI semantic conventions (``gen_ai.operation.name``, ``gen_ai.provider.name``,
``gen_ai.request.model``, ``gen_ai.usage.*``), so any GenAI-aware backend —
Jaeger, Honeycomb, Langfuse, Phoenix, Grafana Tempo — renders them as model
calls without custom mapping.

OpenTelemetry is an **optional** dependency. Without ``opentelemetry-api``
installed every helper here is a no-op that costs one attribute lookup; nothing
in the agent imports OpenTelemetry directly. ``ANSIBLE_HEAL_OTEL=0`` turns it
off even when installed.

Span attributes never carry file *contents*, prompts, completions or
credentials — only names, counts, codes and paths. Transcripts are where the
content goes; traces are shipped to third-party backends.

Exporting: the API alone records nothing. ``configure_from_env()`` (called by
the CLI) installs an SDK tracer provider with an OTLP exporter when
``OTEL_EXPORTER_OTLP_ENDPOINT`` or ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` is set
and the SDK + exporter packages are present (``pip install
'ansible-heal-agent[otel]'``). Anything already configured by the host process
is left alone.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

try:  # pragma: no cover - exercised by whichever branch the env provides
    from opentelemetry import trace as _trace
    from opentelemetry.trace import Status, StatusCode
except ImportError:  # pragma: no cover
    _trace = None
    Status = StatusCode = None

TRACER_NAME = "ansible_heal_agent"
DISABLE_ENV = "ANSIBLE_HEAL_OTEL"


def enabled() -> bool:
    """True when OpenTelemetry is importable and not switched off."""
    if _trace is None:
        return False
    return os.environ.get(DISABLE_ENV, "1").strip().lower() not in {"0", "false", "off"}


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop Nones and coerce values to types OTel accepts."""
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if value is None:
            continue
        if isinstance(value, (bool, int, float, str)):
            out[key] = value
        elif isinstance(value, (list, tuple)):
            out[key] = [str(v) for v in value]
        else:
            out[key] = str(value)
    return out


class _NoopSpan:
    def set_attribute(self, *_a: Any, **_k: Any) -> None:
        pass

    def set_attributes(self, *_a: Any, **_k: Any) -> None:
        pass

    def add_event(self, *_a: Any, **_k: Any) -> None:
        pass


_NOOP = _NoopSpan()


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """Open a span named ``name``; yields it (or a no-op stand-in).

    An exception escaping the block is recorded on the span and marks it as an
    error, then re-raised unchanged.
    """
    if not enabled():
        yield _NOOP
        return
    tracer = _trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(
        name, attributes=_clean(attrs), record_exception=True,
        set_status_on_exception=True,
    ) as current:
        yield current


def set_attrs(target: Any, **attrs: Any) -> None:
    """Set attributes on ``target`` (a span or the no-op), skipping Nones."""
    target.set_attributes(_clean(attrs))


def mark_error(target: Any, message: str) -> None:
    """Mark a span failed without an exception (e.g. an LLM fallback)."""
    if StatusCode is not None and not isinstance(target, _NoopSpan):
        target.set_status(Status(StatusCode.ERROR, message[:200]))


def traced(
    name: str,
    attrs: Callable[..., dict[str, Any]] | None = None,
    result_attrs: Callable[[Any], dict[str, Any]] | None = None,
) -> Callable:
    """Decorator form of :func:`span`.

    ``attrs(*args, **kwargs)`` derives start attributes from the call;
    ``result_attrs(return_value)`` derives attributes from the result. Both
    are guarded: a bug in an attribute extractor never breaks the agent.
    """
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not enabled():
                return fn(*args, **kwargs)
            start: dict[str, Any] = {}
            if attrs:
                try:
                    start = attrs(*args, **kwargs)
                except Exception:  # noqa: BLE001 - telemetry must not break healing
                    start = {}
            with span(name, **start) as current:
                value = fn(*args, **kwargs)
                if result_attrs:
                    try:
                        set_attrs(current, **result_attrs(value))
                    except Exception:  # noqa: BLE001
                        pass
                return value
        return wrapper
    return deco


def configure_from_env() -> bool:
    """Install an OTLP-exporting tracer provider if the env asks for one.

    Returns True if a provider was installed. Never raises.
    """
    if not enabled():
        return False
    if not (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")):
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return False
    current = _trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        return False  # the host process already configured tracing
    provider = TracerProvider(resource=Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "ansible-heal-agent"),
    }))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    _trace.set_tracer_provider(provider)
    return True
