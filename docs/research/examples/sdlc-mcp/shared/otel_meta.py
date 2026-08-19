"""OTEL ↔ MCP `_meta` helpers (SEP-414).

Official MCP Python SDK already supports this:

  await session.call_tool(name, arguments, meta={"traceparent": "00-..."})

`RequestParams.Meta` has ``extra=\"allow\"``, so ``traceparent`` / ``tracestate`` /
``baggage`` pass through (W3C Trace Context).
"""

from __future__ import annotations

from typing import Any

from opentelemetry import context as otel_context
from opentelemetry.propagate import extract, inject


def meta_from_otel(*, lf=None) -> dict[str, Any]:
    """Inject current OTEL (+ optional Langfuse ids) into an MCP ``_meta`` dict."""
    carrier: dict[str, Any] = {}
    inject(carrier)
    if lf is not None:
        try:
            tid = lf.get_current_trace_id()
            oid = lf.get_current_observation_id()
            if tid:
                carrier["langfuse.trace_id"] = tid
            if oid:
                carrier["langfuse.parent_span_id"] = oid
        except Exception:  # noqa: BLE001
            pass
    return carrier


def attach_from_meta(meta: dict[str, Any] | None):
    """Extract parent context from MCP ``_meta`` and attach it. Returns a token."""
    if not meta:
        return otel_context.attach(otel_context.get_current())

    carrier: dict[str, str] = {}
    for key in ("traceparent", "tracestate", "baggage"):
        val = meta.get(key)
        if val:
            carrier[key] = str(val)
    if not carrier:
        return otel_context.attach(otel_context.get_current())
    return otel_context.attach(extract(carrier))


def detach(token) -> None:
    otel_context.detach(token)


def parse_traceparent(traceparent: str | None) -> dict[str, str] | None:
    """W3C ``00-<trace_id>-<span_id>-<flags>`` → Langfuse ``trace_context``."""
    if not traceparent or not isinstance(traceparent, str):
        return None
    parts = traceparent.strip().split("-")
    if len(parts) < 4:
        return None
    _ver, trace_id, span_id, _flags = parts[0], parts[1], parts[2], parts[3]
    if len(trace_id) != 32 or len(span_id) != 16:
        return None
    return {"trace_id": trace_id, "parent_span_id": span_id}


def langfuse_trace_context_from_meta(meta: dict[str, Any] | None) -> dict[str, str] | None:
    """Prefer explicit langfuse ids in ``_meta``, else parse ``traceparent``."""
    if not meta:
        return None
    tid = meta.get("langfuse.trace_id") or meta.get("langfuse_trace_id")
    pid = meta.get("langfuse.parent_span_id") or meta.get("langfuse_parent_span_id")
    if tid and pid:
        return {"trace_id": str(tid), "parent_span_id": str(pid)}
    if tid:
        return {"trace_id": str(tid)}
    return parse_traceparent(meta.get("traceparent"))


def meta_dict_from_request_meta(meta_obj: Any) -> dict[str, Any]:
    """Normalize ``RequestParams.Meta`` (or dict) → plain dict for extract()."""
    if meta_obj is None:
        return {}
    if isinstance(meta_obj, dict):
        return meta_obj
    if hasattr(meta_obj, "model_dump"):
        return meta_obj.model_dump(exclude_none=True)
    out: dict[str, Any] = {}
    for key in ("traceparent", "tracestate", "baggage", "progressToken"):
        if hasattr(meta_obj, key):
            val = getattr(meta_obj, key)
            if val is not None:
                out[key] = val
    extra = getattr(meta_obj, "__pydantic_extra__", None) or getattr(
        meta_obj, "model_extra", None
    )
    if isinstance(extra, dict):
        out.update(extra)
    return out
