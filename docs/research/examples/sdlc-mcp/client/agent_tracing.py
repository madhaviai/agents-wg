"""Instrument LangChain agent LLM turns as Langfuse generations (MCP client)."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse


def _as_dict(obj) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:  # noqa: BLE001
            pass
    out = {}
    for k in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "model_name",
        "model",
        "token_usage",
        "usage",
    ):
        if hasattr(obj, k):
            val = getattr(obj, k)
            if val is not None:
                out[k] = val
    return out


def _usage_and_model(response) -> tuple[dict[str, int] | None, str | None]:
    ai_msg = response
    if hasattr(response, "result") and isinstance(response.result, list) and response.result:
        ai_msg = response.result[0]

    usage_raw = _as_dict(getattr(ai_msg, "usage_metadata", None))
    meta = _as_dict(getattr(ai_msg, "response_metadata", None))
    if not usage_raw:
        usage_raw = _as_dict(meta.get("token_usage") or meta.get("usage"))

    inp = int(usage_raw.get("input_tokens") or usage_raw.get("prompt_tokens") or 0)
    out = int(usage_raw.get("output_tokens") or usage_raw.get("completion_tokens") or 0)
    total = int(usage_raw.get("total_tokens") or (inp + out) or 0)
    usage = None
    if inp or out or total:
        usage = {
            "input": inp,
            "output": out,
            "total": total,
            "prompt_tokens": inp,
            "completion_tokens": out,
        }

    model = meta.get("model_name") or meta.get("model")
    return usage, (str(model) if model else None)


def _parent_ctx(lf) -> dict[str, str] | None:
    if lf is None:
        return None
    try:
        tid = lf.get_current_trace_id()
        oid = lf.get_current_observation_id()
    except Exception:  # noqa: BLE001
        return None
    if not tid:
        return None
    ctx: dict[str, str] = {"trace_id": tid}
    if oid:
        ctx["parent_span_id"] = oid
    return ctx


class AgentLlmTracingMiddleware(AgentMiddleware):
    """Nest ``agent:llm`` generations under the current MCP session span."""

    def __init__(self, lf=None, agent_name: str = "mcp-agent"):
        self.lf = lf
        self.agent_name = agent_name

    def _record(self, span, response) -> None:
        usage, model = _usage_and_model(response)
        update: dict[str, Any] = {}
        if usage:
            update["usage_details"] = usage
        if model:
            update["model"] = model
        try:
            ai = response.result[0] if hasattr(response, "result") and response.result else response
            content = getattr(ai, "content", None)
            if content:
                update["output"] = {"content": str(content)[:2000]}
        except Exception:  # noqa: BLE001
            pass
        if update:
            span.update(**update)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        if self.lf is None:
            return handler(request)
        kwargs: dict[str, Any] = {
            "name": f"{self.agent_name}:llm",
            "as_type": "generation",
            "metadata": {"side": "client", "mode": "mcp-prod"},
        }
        parent = _parent_ctx(self.lf)
        if parent:
            kwargs["trace_context"] = parent
        with self.lf.start_as_current_observation(**kwargs) as span:
            response = handler(request)
            self._record(span, response)
            return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if self.lf is None:
            return await handler(request)
        kwargs: dict[str, Any] = {
            "name": f"{self.agent_name}:llm",
            "as_type": "generation",
            "metadata": {"side": "client", "mode": "mcp-prod"},
        }
        parent = _parent_ctx(self.lf)
        if parent:
            kwargs["trace_context"] = parent
        with self.lf.start_as_current_observation(**kwargs) as span:
            response = await handler(request)
            self._record(span, response)
            return response
