"""Opt-in OpenTelemetry tracing for Hermes (#167).

Local-first and DISABLED unless explicitly configured — AGENTS.md forbids any
telemetry without opt-in gating. When ``telemetry.otel.enabled`` is not set in
config.yaml, every call here is a zero-overhead no-op (it never even imports
opentelemetry). When enabled it emits spans for the boundaries the agent wires
(cron jobs, agent runs, …) to a local console exporter by default, or to an OTLP
endpoint if ``telemetry.otel.exporter: otlp`` (+ endpoint / OTEL_EXPORTER_OTLP_ENDPOINT).

Config (config.yaml):
    telemetry:
      otel:
        enabled: true              # opt-in; default false
        exporter: console | otlp   # default console (local-first)
        endpoint: http://localhost:4318/v1/traces   # otlp only

Design guarantees:
  * No-op + zero overhead when disabled (a single bool check, no OTel import).
  * Telemetry NEVER raises into the caller — a broken exporter must not break
    the agent. Instrumentation errors degrade to no-op.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

_INITED = False
_ENABLED = False
_TRACER = None
_PROVIDER = None


def _otel_config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        return (load_config_readonly().get("telemetry") or {}).get("otel") or {}
    except Exception:
        return {}


def _profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def is_enabled() -> bool:
    """Opt-in only. Cached after first init."""
    if _INITED:
        return _ENABLED
    return bool(_otel_config().get("enabled", False))


def _build_exporter(cfg: dict):
    kind = str(cfg.get("exporter") or "console").lower()
    if kind == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = cfg.get("endpoint") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        return OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter

    return ConsoleSpanExporter()


def _ensure_init() -> None:
    global _INITED, _ENABLED, _TRACER, _PROVIDER
    if _INITED:
        return
    _INITED = True
    cfg = _otel_config()
    if not cfg.get("enabled", False):
        _ENABLED = False
        return
    # Opt-in backend: lazy-install OTel at first enabled use (prompt=False so a
    # cron tick never blocks on input()). Any failure degrades to no-op below.
    try:
        from tools import lazy_deps

        lazy_deps.ensure("telemetry.otel", prompt=False)
    except Exception:
        pass
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create(
                {"service.name": "hermes-agent", "hermes.profile": _profile_name()}
            )
        )
        provider.add_span_processor(BatchSpanProcessor(_build_exporter(cfg)))
        trace.set_tracer_provider(provider)
        _PROVIDER = provider
        _TRACER = trace.get_tracer("hermes")
        _ENABLED = True
    except Exception:
        _ENABLED = False  # OTel missing / misconfigured → silent no-op


@contextlib.contextmanager
def span(name: str, **attributes: Any):
    """Trace ``name`` with safe ``hermes.*`` attributes. No-op when disabled.
    Caller exceptions propagate (recorded as span error); telemetry errors never
    propagate."""
    _ensure_init()
    sp = None
    cm = None
    if _ENABLED and _TRACER is not None:
        try:
            cm = _TRACER.start_as_current_span(name)
            sp = cm.__enter__()
            for k, v in attributes.items():
                if v is None:
                    continue
                try:
                    sp.set_attribute(
                        f"hermes.{k}", v if isinstance(v, (str, int, float, bool)) else str(v)
                    )
                except Exception:
                    pass
        except Exception:
            cm = sp = None  # telemetry broke → degrade to no-op
    try:
        yield sp
    except Exception as e:
        if sp is not None:
            try:
                from opentelemetry.trace import Status, StatusCode

                sp.set_status(Status(StatusCode.ERROR, str(e)[:200]))
            except Exception:
                pass
        raise
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass


def set_attributes(**attributes: Any) -> None:
    """Attach attributes to the current span if one is active (no-op otherwise)."""
    if not _ENABLED:
        return
    try:
        from opentelemetry import trace

        sp = trace.get_current_span()
        if sp is None:
            return
        for k, v in attributes.items():
            if v is not None:
                try:
                    sp.set_attribute(
                        f"hermes.{k}", v if isinstance(v, (str, int, float, bool)) else str(v)
                    )
                except Exception:
                    pass
    except Exception:
        pass


# ── Test hooks ────────────────────────────────────────────────────────────────
def _reset_for_test() -> None:
    global _INITED, _ENABLED, _TRACER, _PROVIDER
    _INITED, _ENABLED, _TRACER, _PROVIDER = False, False, None, None


def _force_enable_with_exporter(exporter) -> None:
    """Enable tracing with an injected SpanExporter (in-memory) — tests only.
    Uses a provider-local tracer (not the process-global) to stay isolated."""
    global _INITED, _ENABLED, _TRACER, _PROVIDER
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "hermes-agent-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _PROVIDER = provider
    _TRACER = provider.get_tracer("hermes-test")
    _INITED, _ENABLED = True, True
