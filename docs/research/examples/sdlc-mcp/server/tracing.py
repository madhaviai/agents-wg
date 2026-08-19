"""Server executor spans — nest under client via SEP-414 ``_meta`` only.

Never create a Langfuse root on the server: if parent context is missing, skip
the span and just run the handler (avoids one-orphan-trace-per-call).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from typing import Any

from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.agents import GetAgentRequestParams, GetAgentResult, ListAgentsResult
from mcp_types import (
    CallToolRequestParams,
    CallToolResult,
    GetPromptRequestParams,
    GetPromptResult,
    InputRequiredResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    ReadResourceResult,
    TextContent,
)

from shared.langfuse_client import flush, get_langfuse, langfuse_server_enabled
from shared.otel_meta import (
    attach_from_meta,
    detach,
    langfuse_trace_context_from_meta,
    meta_dict_from_request_meta,
)

_MAX = 6000


def _clip(data: Any, limit: int = _MAX) -> Any:
    text = str(data)
    return text if len(text) <= limit else text[:limit] + "... [truncated]"


def _preview_tool_result(result: Sequence | dict[str, Any] | Iterable) -> Any:
    if isinstance(result, dict):
        return _clip(result)
    if isinstance(result, CallToolResult):
        parts = []
        for block in result.content or []:
            if isinstance(block, TextContent) or getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", "") or "")
            else:
                parts.append(str(block))
        text = "\n".join(parts)
        try:
            import json

            return json.loads(text)
        except Exception:  # noqa: BLE001
            return text if len(text) <= _MAX else text[:_MAX] + "... [truncated]"
    parts: list[str] = []
    for block in result:
        if isinstance(block, TextContent) or getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
        elif isinstance(block, ReadResourceContents) or hasattr(block, "content"):
            parts.append(str(getattr(block, "content", block)))
        else:
            parts.append(str(block))
    text = "\n".join(parts)
    try:
        import json

        return json.loads(text)
    except Exception:  # noqa: BLE001
        return text if len(text) <= _MAX else text[:_MAX] + "... [truncated]"


def _meta_from_ctx(ctx: ServerRequestContext) -> dict[str, Any]:
    return meta_dict_from_request_meta(getattr(ctx, "meta", None))


@contextmanager
def _server_span(lf, name: str, *, input: Any, metadata: dict, meta: dict):
    """Only emit when we can nest under the client (SEP-414 parent present)."""
    tctx = langfuse_trace_context_from_meta(meta)
    if lf is None or not tctx:
        yield None
        return
    with lf.start_as_current_observation(
        name=name,
        as_type="span",
        input=input,
        metadata={"mode": "mcp-prod", "side": "server", **metadata},
        trace_context=tctx,
    ) as span:
        yield span
    flush()


class TracedMCPServer(MCPServer):
    """Executor spans nested under client RPCs (production SEP-414 mode)."""

    async def _handle_list_tools(self, ctx, params: PaginatedRequestParams | None):
        if not langfuse_server_enabled():
            return await super()._handle_list_tools(ctx, params)
        lf = get_langfuse()
        meta = _meta_from_ctx(ctx)
        token = attach_from_meta(meta)
        try:
            with _server_span(
                lf,
                "mcp.server:tools/list",
                input={"method": "tools/list"},
                metadata={"mcp.method": "tools/list"},
                meta=meta,
            ) as span:
                result = await super()._handle_list_tools(ctx, params)
                if span is not None:
                    span.update(
                        output={
                            "count": len(result.tools),
                            "names": [t.name for t in result.tools],
                        }
                    )
                return result
        finally:
            detach(token)

    async def _handle_list_agents(self, ctx, params: PaginatedRequestParams | None) -> ListAgentsResult:
        if not langfuse_server_enabled():
            return await super()._handle_list_agents(ctx, params)
        lf = get_langfuse()
        meta = _meta_from_ctx(ctx)
        token = attach_from_meta(meta)
        try:
            with _server_span(
                lf,
                "mcp.server:agents/list",
                input={"method": "agents/list"},
                metadata={"mcp.method": "agents/list"},
                meta=meta,
            ) as span:
                result = await super()._handle_list_agents(ctx, params)
                if span is not None:
                    span.update(
                        output={
                            "count": len(result.agents),
                            "names": [a.name for a in result.agents],
                        }
                    )
                return result
        finally:
            detach(token)

    async def _handle_get_agent(self, ctx, params: GetAgentRequestParams) -> GetAgentResult:
        if not langfuse_server_enabled():
            return await super()._handle_get_agent(ctx, params)
        lf = get_langfuse()
        meta = _meta_from_ctx(ctx)
        token = attach_from_meta(meta)
        try:
            with _server_span(
                lf,
                f"mcp.server:agents/get {params.name}",
                input={"method": "agents/get", "params": {"name": params.name}},
                metadata={"mcp.method": "agents/get", "mcp.agent.name": params.name},
                meta=meta,
            ) as span:
                result = await super()._handle_get_agent(ctx, params)
                if span is not None:
                    span.update(
                        output={
                            "agent": result.agent,
                            "tool_count": len(result.tools),
                            "tools": [t.name for t in result.tools],
                        }
                    )
                return result
        finally:
            detach(token)

    async def _handle_list_resources(self, ctx, params: PaginatedRequestParams | None):
        if not langfuse_server_enabled():
            return await super()._handle_list_resources(ctx, params)
        lf = get_langfuse()
        meta = _meta_from_ctx(ctx)
        token = attach_from_meta(meta)
        try:
            with _server_span(
                lf,
                "mcp.server:resources/list",
                input={"method": "resources/list"},
                metadata={"mcp.method": "resources/list"},
                meta=meta,
            ) as span:
                result = await super()._handle_list_resources(ctx, params)
                if span is not None:
                    span.update(
                        output={
                            "count": len(result.resources),
                            "uris": [str(r.uri) for r in result.resources],
                        }
                    )
                return result
        finally:
            detach(token)

    async def _handle_list_resource_templates(self, ctx, params: PaginatedRequestParams | None):
        if not langfuse_server_enabled():
            return await super()._handle_list_resource_templates(ctx, params)
        lf = get_langfuse()
        meta = _meta_from_ctx(ctx)
        token = attach_from_meta(meta)
        try:
            with _server_span(
                lf,
                "mcp.server:resources/templates/list",
                input={"method": "resources/templates/list"},
                metadata={"mcp.method": "resources/templates/list"},
                meta=meta,
            ) as span:
                result = await super()._handle_list_resource_templates(ctx, params)
                if span is not None:
                    templates = result.resource_templates
                    span.update(
                        output={
                            "count": len(templates),
                            "uriTemplates": [str(t.uri_template) for t in templates],
                        }
                    )
                return result
        finally:
            detach(token)

    async def _handle_list_prompts(self, ctx, params: PaginatedRequestParams | None):
        if not langfuse_server_enabled():
            return await super()._handle_list_prompts(ctx, params)
        lf = get_langfuse()
        meta = _meta_from_ctx(ctx)
        token = attach_from_meta(meta)
        try:
            with _server_span(
                lf,
                "mcp.server:prompts/list",
                input={"method": "prompts/list"},
                metadata={"mcp.method": "prompts/list"},
                meta=meta,
            ) as span:
                result = await super()._handle_list_prompts(ctx, params)
                if span is not None:
                    span.update(
                        output={
                            "count": len(result.prompts),
                            "names": [p.name for p in result.prompts],
                        }
                    )
                return result
        finally:
            detach(token)

    async def _handle_get_prompt(
        self, ctx, params: GetPromptRequestParams
    ) -> GetPromptResult | InputRequiredResult:
        if not langfuse_server_enabled():
            return await super()._handle_get_prompt(ctx, params)
        lf = get_langfuse()
        meta = _meta_from_ctx(ctx)
        token = attach_from_meta(meta)
        try:
            with _server_span(
                lf,
                f"mcp.server:prompts/get {params.name}",
                input={
                    "method": "prompts/get",
                    "params": {"name": params.name, "arguments": params.arguments},
                },
                metadata={"mcp.method": "prompts/get", "mcp.prompt.name": params.name},
                meta=meta,
            ) as span:
                result = await super()._handle_get_prompt(ctx, params)
                if span is not None:
                    span.update(output=_clip(result))
                return result
        finally:
            detach(token)

    async def _handle_call_tool(
        self, ctx, params: CallToolRequestParams
    ) -> CallToolResult | InputRequiredResult:
        if not langfuse_server_enabled():
            return await super()._handle_call_tool(ctx, params)

        meta = _meta_from_ctx(ctx)
        token = attach_from_meta(meta)
        lf = get_langfuse()
        try:
            req = {
                "method": "tools/call",
                "params": {
                    "name": params.name,
                    "arguments": params.arguments,
                    "_meta": meta or None,
                },
            }
            with _server_span(
                lf,
                f"mcp.server:tools/call {params.name}",
                input=req,
                metadata={"mcp.method": "tools/call", "mcp.tool.name": params.name},
                meta=meta,
            ) as span:
                try:
                    result = await super()._handle_call_tool(ctx, params)
                    if span is not None:
                        span.update(output=_preview_tool_result(result))
                    return result
                except Exception as exc:  # noqa: BLE001
                    if span is not None:
                        span.update(output={"error": str(exc)}, level="ERROR")
                    raise
        finally:
            detach(token)

    async def _handle_read_resource(
        self, ctx, params: ReadResourceRequestParams
    ) -> ReadResourceResult | InputRequiredResult:
        if not langfuse_server_enabled():
            return await super()._handle_read_resource(ctx, params)

        meta = _meta_from_ctx(ctx)
        token = attach_from_meta(meta)
        lf = get_langfuse()
        uri_s = str(params.uri)
        try:
            req = {
                "method": "resources/read",
                "params": {"uri": uri_s, "_meta": meta or None},
            }
            with _server_span(
                lf,
                f"mcp.server:resources/read {uri_s}",
                input=req,
                metadata={"mcp.method": "resources/read"},
                meta=meta,
            ) as span:
                try:
                    result = await super()._handle_read_resource(ctx, params)
                    if span is not None:
                        span.update(output=_clip(result))
                    return result
                except Exception as exc:  # noqa: BLE001
                    if span is not None:
                        span.update(output={"error": str(exc)}, level="ERROR")
                    raise
        finally:
            detach(token)


# Back-compat alias used by older imports
TracedFastMCP = TracedMCPServer
