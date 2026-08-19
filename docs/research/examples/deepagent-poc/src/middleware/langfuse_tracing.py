"""Langfuse tracing — deep-agent routing sequence + tool spans.

Demo tree (one trace per user turn):

  deepagent-session
  ├── 00.supervisor:route          (generation + tokens)
  ├── 01.handoff→insights-agent    (agent)
  │     ├── insights-agent:llm
  │     └── insights-agent:tool:…
  ├── 02.handoff→research-agent
  ├── 03.handoff→workflow-agent
  └── 98.supervisor:synthesize

Compatible with langfuse SDK v4 (`start_as_current_observation`).
"""

from __future__ import annotations

import contextvars
import os
import threading
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langfuse import Langfuse
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest

_client_cache: dict[str, Langfuse] = {}
_client_cache_lock = threading.Lock()
_route_by_trace: dict[str, list[dict[str, Any]]] = {}
_handoff_by_trace: dict[str, int] = {}

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "poc_langfuse_trace_id", default=None
)
_trace_owner_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "poc_langfuse_trace_owner", default=None
)
_trace_input_set_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "poc_langfuse_trace_input_set", default=False
)
# Explicit parent for nested agents (task() may lose OTEL current across invoke)
_force_parent_oid_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "poc_langfuse_force_parent_oid", default=None
)

_MAX_CONTENT_LEN = 4000


def _enabled() -> bool:
    return os.getenv("LANGFUSE_ENABLED", "false").lower() in ("1", "true", "yes")


def _make_client() -> Langfuse | None:
    if not _enabled():
        return None
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        print("[langfuse] LANGFUSE_ENABLED but keys missing — tracing off")
        return None
    host = (
        os.getenv("LANGFUSE_HOST")
        or os.getenv("LANGFUSE_BASE_URL")
        or "https://cloud.langfuse.com"
    )
    return Langfuse(public_key=public_key, secret_key=secret_key, host=host)


def _get_or_create_client(trace_id: str) -> Langfuse | None:
    with _client_cache_lock:
        if trace_id in _client_cache:
            return _client_cache[trace_id]
    client = _make_client()
    if client is not None:
        with _client_cache_lock:
            _client_cache[trace_id] = client
    return client


def _child_trace_context(client: Langfuse, trace_id: str) -> dict[str, str]:
    """Nest under current (or forced) observation — never orphan with bare trace_id."""
    ctx: dict[str, str] = {"trace_id": trace_id}
    parent = _force_parent_oid_var.get(None)
    if not parent:
        try:
            parent = client.get_current_observation_id()
        except Exception:  # noqa: BLE001
            parent = None
    if parent:
        ctx["parent_span_id"] = parent
    return ctx


def _usage_and_model(response) -> tuple[dict[str, int] | None, str | None]:
    """Pull token usage + model from LangChain / Groq AIMessage."""
    ai_msg = response
    if hasattr(response, "result") and isinstance(response.result, list) and response.result:
        ai_msg = response.result[0]

    def _as_dict(obj) -> dict:
        if obj is None:
            return {}
        if isinstance(obj, dict):
            return obj
        # LangChain UsageMetadata / pydantic
        if hasattr(obj, "model_dump"):
            try:
                return obj.model_dump()
            except Exception:  # noqa: BLE001
                pass
        out = {}
        for k in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens"):
            if hasattr(obj, k):
                val = getattr(obj, k)
                if val is not None:
                    out[k] = val
        return out

    usage_raw = _as_dict(getattr(ai_msg, "usage_metadata", None))
    meta = _as_dict(getattr(ai_msg, "response_metadata", None))
    if not usage_raw:
        usage_raw = _as_dict(meta.get("token_usage") or meta.get("usage"))

    inp = int(usage_raw.get("input_tokens") or usage_raw.get("prompt_tokens") or 0)
    out = int(usage_raw.get("output_tokens") or usage_raw.get("completion_tokens") or 0)
    total = int(usage_raw.get("total_tokens") or (inp + out) or 0)
    usage = None
    if inp or out or total:
        # Langfuse accepts both naming styles; send both for UI compatibility
        usage = {
            "input": inp,
            "output": out,
            "total": total,
            "prompt_tokens": inp,
            "completion_tokens": out,
        }

    model = meta.get("model_name") or meta.get("model")
    return usage, (str(model) if model else None)


def _route_seq(trace_id: str | None = None) -> list[dict[str, Any]]:
    tid = trace_id or _trace_id_var.get(None) or "_"
    with _client_cache_lock:
        if tid not in _route_by_trace:
            _route_by_trace[tid] = []
        return _route_by_trace[tid]


def _append_route(event: str, **extra: Any) -> int:
    tid = _trace_id_var.get(None) or "_"
    seq = _route_seq(tid)
    step = len(seq) + 1
    seq.append({"step": step, "event": event, **extra})
    return step


def get_route_sequence(trace_id: str | None = None) -> list[dict[str, Any]]:
    tid = trace_id or _trace_id_var.get(None)
    if not tid:
        return []
    with _client_cache_lock:
        return list(_route_by_trace.get(tid) or [])


def begin_session(
    *,
    trace_id: str,
    user_input: str,
    mode: str = "deep",
    user_id: str = "poc-user",
    session_id: str | None = None,
    tags: list[str] | None = None,
):
    """Open root ``deepagent-session`` span. Returns (client, session_state)."""
    client = _get_or_create_client(trace_id)
    if client is None:
        return None, None
    _trace_id_var.set(trace_id)
    _trace_owner_var.set("supervisor")
    _trace_input_set_var.set(False)
    tags = list(tags or [])
    if f"mode:{mode}" not in tags:
        tags.append(f"mode:{mode}")

    from langfuse import propagate_attributes

    root_cm = client.start_as_current_observation(
        name="deepagent-session",
        as_type="span",
        trace_context={"trace_id": trace_id},
        input=user_input,
        metadata={"mode": mode, "tags": tags},
    )
    span = root_cm.__enter__()
    prop_cm = propagate_attributes(
        user_id=user_id,
        session_id=session_id,
        tags=tags,
        trace_name="deepagent-session",
        metadata={"mode": mode},
    )
    prop_cm.__enter__()
    return client, {"root_cm": root_cm, "span": span, "prop_cm": prop_cm}


def end_session(
    client: Langfuse | None,
    session_cm,
    *,
    trace_id: str,
    output: str = "",
    input_text: str = "",
) -> None:
    """Close root span, attach route summary, flush."""
    route = get_route_sequence(trace_id)
    route_summary = " → ".join(
        f"{r['step']}.{r['event']}" + (f"({r['target']})" if r.get("target") else "")
        for r in route
    )
    if session_cm is not None:
        span = session_cm.get("span")
        try:
            if span is not None:
                span.update(
                    output={"answer": output, "route_summary": route_summary},
                    metadata={"route_sequence": route, "route_summary": route_summary},
                )
                if hasattr(span, "set_trace_io"):
                    span.set_trace_io(
                        input=input_text or None,
                        output={"answer": output, "route_summary": route_summary},
                    )
        except Exception:  # noqa: BLE001
            pass
        for key in ("prop_cm", "root_cm"):
            cm = session_cm.get(key)
            if cm is None:
                continue
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
    if client is not None:
        try:
            client.flush()
        except Exception:  # noqa: BLE001
            pass
    if route_summary:
        print(f"[langfuse] route: {route_summary}")
    with _client_cache_lock:
        _client_cache.pop(trace_id, None)
    _reset_vars(trace_id)


def finalize_and_flush(trace_id: str | None, *, output: str = "", input_text: str = "") -> None:
    """Backward-compatible flush when session root was not used."""
    if not trace_id:
        return
    with _client_cache_lock:
        client = _client_cache.pop(trace_id, None)
    route = get_route_sequence(trace_id)
    if client is None:
        _reset_vars(trace_id)
        return
    try:
        route_summary = " → ".join(
            f"{r['step']}.{r['event']}" + (f"({r['target']})" if r.get("target") else "")
            for r in route
        )
        parent = None
        try:
            parent = client.get_current_observation_id()
        except Exception:  # noqa: BLE001
            pass
        tctx: dict[str, str] = {"trace_id": trace_id}
        if parent:
            tctx["parent_span_id"] = parent
        with client.start_as_current_observation(
            name="99.supervisor:final",
            as_type="span",
            trace_context=tctx,
            input={"user": input_text, "route_sequence": route},
            output={"answer": output, "route_summary": route_summary},
            metadata={"route_sequence": route, "route_summary": route_summary},
        ):
            pass
        try:
            client.update_current_trace(
                name="deepagent-session",
                metadata={"route_summary": route_summary},
            )
        except Exception:  # noqa: BLE001
            pass
        client.flush()
        if route_summary:
            print(f"[langfuse] route: {route_summary}")
    except Exception as exc:  # noqa: BLE001
        print(f"[langfuse] finalize/flush failed: {exc}")
    finally:
        _reset_vars(trace_id)


def _reset_vars(trace_id: str | None = None) -> None:
    _trace_id_var.set(None)
    _trace_owner_var.set(None)
    _trace_input_set_var.set(False)
    _force_parent_oid_var.set(None)
    if trace_id:
        with _client_cache_lock:
            _route_by_trace.pop(trace_id, None)
            _handoff_by_trace.pop(trace_id, None)


def _truncate(text: str, limit: int = _MAX_CONTENT_LEN) -> str:
    if len(text) > limit:
        return text[:limit] + "... [truncated]"
    return text


class LangfuseTracingMiddleware(AgentMiddleware):
    """Wrap model + tool calls; nest under session / handoff parents."""

    def __init__(self, agent_name: str = "supervisor", agent_prompt: str | None = None):
        self.agent_name = agent_name
        self.agent_prompt = agent_prompt

    def _get_trace_context(self) -> dict[str, Any] | None:
        if not _enabled():
            return None
        try:
            config = get_config()
        except Exception:
            config = {}
        configurable = config.get("configurable", {}) if isinstance(config, dict) else {}

        trace_id = (
            configurable.get("trace_id")
            or _trace_id_var.get(None)
            or Langfuse.create_trace_id()
        )
        _trace_id_var.set(trace_id)
        if _trace_owner_var.get(None) is None:
            _trace_owner_var.set(self.agent_name)

        # Optional forced parent from task() handoff
        forced = configurable.get("parent_observation_id")
        if forced:
            _force_parent_oid_var.set(str(forced))

        client = _get_or_create_client(trace_id)
        if client is None:
            return None

        return {
            "client": client,
            "trace_id": trace_id,
            "session_id": configurable.get("thread_id"),
            "user_id": configurable.get("user_id", "poc-user"),
            "original_input": configurable.get("original_user_input", ""),
            "mode": configurable.get("mode", "deep"),
            "tags": configurable.get("tags") or [],
        }

    @staticmethod
    def _unwrap_ai_message(response):
        if hasattr(response, "result") and isinstance(response.result, list) and response.result:
            return response.result[0]
        return response

    def _serialize_response(self, response) -> dict[str, Any]:
        ai_msg = self._unwrap_ai_message(response)
        out: dict[str, Any] = {}
        content = getattr(ai_msg, "content", "")
        if content:
            out["content"] = _truncate(str(content))
        tool_calls = getattr(ai_msg, "tool_calls", None)
        if tool_calls:
            out["tool_calls"] = [
                {
                    "name": tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?"),
                    "args": tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {}),
                }
                for tc in tool_calls
            ]
        return out

    def _extract_route_targets(self, response) -> list[str]:
        ai_msg = self._unwrap_ai_message(response)
        tool_calls = getattr(ai_msg, "tool_calls", None) or []
        targets = []
        for tc in tool_calls:
            name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            if name == "task" and isinstance(args, dict):
                targets.append(args.get("subagent_type") or args.get("agent") or "?")
        return targets

    def _llm_span_name(self, response=None) -> str:
        if self.agent_name != "supervisor":
            return f"{self.agent_name}:llm"
        targets = self._extract_route_targets(response) if response is not None else []
        if targets:
            return "00.supervisor:route"
        tid = _trace_id_var.get(None) or ""
        if _handoff_by_trace.get(tid, 0) > 0:
            return "98.supervisor:synthesize"
        return "00.supervisor:llm"

    def _finalize_model_span(self, span, ctx: dict, request: ModelRequest, response) -> None:
        if span is None:
            return
        messages = getattr(request, "messages", None) or []
        targets = self._extract_route_targets(response)
        meta: dict[str, Any] = {
            "agent": self.agent_name,
            "agent_prompt": (self.agent_prompt or "")[:500],
        }
        if targets:
            meta["routing_decision"] = targets
            meta["phase"] = "route"
            step = _append_route(
                "supervisor:route",
                targets=targets,
                agent=self.agent_name,
            )
            meta["route_step"] = step
            print(f"[route] step={step} supervisor → {', '.join(targets)}")
        elif self.agent_name == "supervisor" and _handoff_by_trace.get(
            _trace_id_var.get(None) or "", 0
        ) > 0:
            meta["phase"] = "synthesize"
            _append_route("supervisor:synthesize", agent=self.agent_name)
        else:
            meta["phase"] = "llm"

        usage, model = _usage_and_model(response)
        update_kwargs: dict[str, Any] = {
            "input": {"message_count": len(messages), "agent": self.agent_name},
            "output": self._serialize_response(response),
            "metadata": meta,
        }
        if usage:
            update_kwargs["usage_details"] = usage
        if model:
            update_kwargs["model"] = model
        span.update(**update_kwargs)

        if _trace_owner_var.get(None) != self.agent_name:
            return
        if _trace_input_set_var.get(False):
            return
        _trace_input_set_var.set(True)

        original = ctx.get("original_input") or ""
        if not original:
            for m in reversed(messages):
                if getattr(m, "type", None) == "human":
                    original = str(getattr(m, "content", "") or "")
                    break

        if hasattr(span, "set_trace_io"):
            span.set_trace_io(input=original)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        ctx = self._get_trace_context()
        if ctx is None:
            return handler(request)
        with ctx["client"].start_as_current_observation(
            name=f"{self.agent_name}:llm",
            as_type="generation",
            trace_context=_child_trace_context(ctx["client"], ctx["trace_id"]),
            metadata={"agent": self.agent_name},
        ) as span:
            response = handler(request)
            better = self._llm_span_name(response)
            try:
                span.update(name=better)
            except Exception:  # noqa: BLE001
                pass
            self._finalize_model_span(span, ctx, request, response)
            return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        ctx = self._get_trace_context()
        if ctx is None:
            return await handler(request)
        with ctx["client"].start_as_current_observation(
            name=f"{self.agent_name}:llm",
            as_type="generation",
            trace_context=_child_trace_context(ctx["client"], ctx["trace_id"]),
            metadata={"agent": self.agent_name},
        ) as span:
            response = await handler(request)
            better = self._llm_span_name(response)
            try:
                span.update(name=better)
            except Exception:  # noqa: BLE001
                pass
            self._finalize_model_span(span, ctx, request, response)
            return response

    def _tool_span_name(self, request: ToolCallRequest) -> tuple[str, str | None, dict]:
        tool_name = request.tool_call.get("name", "unknown_tool")
        args = request.tool_call.get("args", {})
        if tool_name == "task" and isinstance(args, dict):
            target = args.get("subagent_type") or args.get("agent") or "unknown"
            tid = _trace_id_var.get(None) or "_"
            with _client_cache_lock:
                step = _handoff_by_trace.get(tid, 0) + 1
                _handoff_by_trace[tid] = step
            _append_route(
                "handoff",
                target=target,
                description=(args.get("description") or "")[:300],
                from_agent=self.agent_name,
            )
            print(f"[handoff] {step:02d}.handoff→{target}")
            return (
                f"{step:02d}.handoff→{target}",
                target,
                {
                    "phase": "handoff",
                    "from": self.agent_name,
                    "to": target,
                    "handoff_step": step,
                    "task_description": (args.get("description") or "")[:500],
                },
            )
        return (
            f"{self.agent_name}:tool:{tool_name}",
            None,
            {"phase": "tool", "agent": self.agent_name, "tool": tool_name},
        )

    def wrap_tool_call(self, request: ToolCallRequest, handler):
        ctx = self._get_trace_context()
        if ctx is None:
            return handler(request)
        span_name, target, meta = self._tool_span_name(request)
        as_type = "agent" if target else "tool"
        with ctx["client"].start_as_current_observation(
            name=span_name,
            as_type=as_type,
            trace_context=_child_trace_context(ctx["client"], ctx["trace_id"]),
            input=request.tool_call.get("args", {}),
            metadata=meta,
        ) as span:
            token = None
            if target:
                # Force subagent spans under this handoff even if OTEL context drops
                try:
                    oid = ctx["client"].get_current_observation_id()
                except Exception:  # noqa: BLE001
                    oid = None
                if oid:
                    token = _force_parent_oid_var.set(oid)
            try:
                result = handler(request)
                content = getattr(result, "content", None)
                out = _truncate(str(content)) if content is not None else str(result)[:500]
                span.update(output=out, metadata={**meta, "ok": True})
                if target:
                    _append_route("handoff_return", target=target, output_preview=out[:200])
                return result
            finally:
                if token is not None:
                    _force_parent_oid_var.reset(token)

    async def awrap_tool_call(self, request: ToolCallRequest, handler):
        ctx = self._get_trace_context()
        if ctx is None:
            return await handler(request)
        span_name, target, meta = self._tool_span_name(request)
        as_type = "agent" if target else "tool"
        with ctx["client"].start_as_current_observation(
            name=span_name,
            as_type=as_type,
            trace_context=_child_trace_context(ctx["client"], ctx["trace_id"]),
            input=request.tool_call.get("args", {}),
            metadata=meta,
        ) as span:
            token = None
            if target:
                try:
                    oid = ctx["client"].get_current_observation_id()
                except Exception:  # noqa: BLE001
                    oid = None
                if oid:
                    token = _force_parent_oid_var.set(oid)
            try:
                result = await handler(request)
                content = getattr(result, "content", None)
                out = _truncate(str(content)) if content is not None else str(result)[:500]
                span.update(output=out, metadata={**meta, "ok": True})
                if target:
                    _append_route("handoff_return", target=target, output_preview=out[:200])
                return result
            finally:
                if token is not None:
                    _force_parent_oid_var.reset(token)
