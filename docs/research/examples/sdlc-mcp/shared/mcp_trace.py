"""Production-like MCP wire spans (SEP-414).

Client: one span per JSON-RPC (caller).
Server: nested executor span when ``_meta`` carries parent (no orphan roots).

``_meta`` is injected *inside* the client span so the server parents under
``mcp:tools/call …``, not under the session root.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Awaitable, Callable

from mcp import types
from pydantic import AnyUrl

from shared.otel_meta import meta_from_otel

_MAX = 6000


def _clip(data: Any, limit: int = _MAX) -> Any:
    try:
        s = json.dumps(data, default=str)
    except TypeError:
        s = str(data)
    if len(s) <= limit:
        try:
            return json.loads(s) if isinstance(data, (dict, list)) else data
        except Exception:  # noqa: BLE001
            return data
    return s[:limit] + "... [truncated]"


def _result_payload(result) -> dict[str, Any]:
    contents: list[Any] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            try:
                contents.append(json.loads(text))
            except json.JSONDecodeError:
                contents.append(text)
        else:
            contents.append(str(block))
    return {
        "isError": bool(
            getattr(result, "isError", None)
            if getattr(result, "isError", None) is not None
            else getattr(result, "is_error", False)
        ),
        "content": contents if len(contents) != 1 else contents[0],
    }


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


def _page_params(meta: dict[str, Any] | None) -> types.PaginatedRequestParams | None:
    if not meta:
        return None
    # mcp v2: RequestParams.meta is RequestParamsMeta (TypedDict) / plain dict
    return types.PaginatedRequestParams(_meta=meta)  # type: ignore[arg-type]


@contextmanager
def _span(
    lf,
    name: str,
    *,
    input: Any = None,
    metadata: dict | None = None,
    as_type: str = "span",
):
    if lf is None:
        yield None
        return
    kwargs: dict[str, Any] = {
        "name": name,
        "as_type": as_type,
        "input": _clip(input) if input is not None else None,
        "metadata": {
            "mode": "mcp-prod",
            "side": "client",
            **(metadata or {}),
        },
    }
    parent = _parent_ctx(lf)
    if parent:
        kwargs["trace_context"] = parent
    with lf.start_as_current_observation(**kwargs) as span:
        yield span


class TracedMcpSession:
    """Client-side MCP RPC spans + SEP-414 ``_meta`` injection."""

    def __init__(self, session, lf=None):
        self.session = session
        self.lf = lf

    async def _run_span(
        self,
        *,
        method: str,
        span_name: str,
        as_type: str = "span",
        extra_meta: dict | None = None,
        invoke: Callable[[dict[str, Any] | None], Awaitable[Any]],
        shape_request: Callable[[dict[str, Any] | None], Any],
        shape_output: Callable[[Any], Any],
    ):
        t0 = time.perf_counter()
        with _span(
            self.lf,
            span_name,
            input={"method": method},
            metadata={"mcp.method": method, **(extra_meta or {})},
            as_type=as_type,
        ) as span:
            # Inject *inside* this span so server parents under it
            wire_meta = meta_from_otel(lf=self.lf) or None
            if span is not None:
                span.update(input=_clip(shape_request(wire_meta)))
            result = await invoke(wire_meta)
            out = shape_output(result)
            if isinstance(out, dict):
                out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            if span is not None:
                span.update(output=_clip(out))
            return result

    async def initialize(self):
        async def invoke(_meta):
            return await self.session.initialize()

        def req(_meta):
            return {"method": "initialize"}

        def out(result):
            server_info = getattr(result, "serverInfo", None) or getattr(
                result, "server_info", None
            )
            return {
                "protocolVersion": getattr(result, "protocolVersion", None)
                or getattr(result, "protocol_version", None),
                "serverInfo": {
                    "name": getattr(server_info, "name", None) if server_info else None,
                    "version": getattr(server_info, "version", None) if server_info else None,
                },
                "instructions": getattr(result, "instructions", None),
            }

        return await self._run_span(
            method="initialize",
            span_name="mcp:initialize",
            invoke=invoke,
            shape_request=req,
            shape_output=out,
        )

    async def send_ping(self):
        async def invoke(_meta):
            return await self.session.send_ping()

        return await self._run_span(
            method="ping",
            span_name="mcp:ping",
            invoke=invoke,
            shape_request=lambda _m: {"method": "ping"},
            shape_output=lambda _r: {"ok": True},
        )

    async def list_tools(self):
        async def invoke(meta):
            return await self.session.list_tools(params=_page_params(meta))

        def req(meta):
            return {"method": "tools/list", "params": {"_meta": meta}}

        def out(result):
            tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": getattr(t, "inputSchema", None)
                    or getattr(t, "input_schema", None),
                }
                for t in result.tools
            ]
            return {"count": len(tools), "tools": tools}

        return await self._run_span(
            method="tools/list",
            span_name="mcp:tools/list",
            invoke=invoke,
            shape_request=req,
            shape_output=out,
        )

    async def list_resources(self):
        async def invoke(meta):
            return await self.session.list_resources(params=_page_params(meta))

        def req(meta):
            return {"method": "resources/list", "params": {"_meta": meta}}

        def out(result):
            resources = [
                {
                    "uri": str(getattr(r, "uri", "")),
                    "name": getattr(r, "name", None),
                    "description": getattr(r, "description", None),
                }
                for r in (result.resources or [])
            ]
            return {"count": len(resources), "resources": resources}

        return await self._run_span(
            method="resources/list",
            span_name="mcp:resources/list",
            invoke=invoke,
            shape_request=req,
            shape_output=out,
        )

    async def list_resource_templates(self):
        async def invoke(meta):
            return await self.session.list_resource_templates(params=_page_params(meta))

        def req(meta):
            return {"method": "resources/templates/list", "params": {"_meta": meta}}

        def out(result):
            templates_raw = (
                getattr(result, "resourceTemplates", None)
                or getattr(result, "resource_templates", None)
                or []
            )
            templates = [
                {
                    "uriTemplate": str(
                        getattr(t, "uriTemplate", None)
                        or getattr(t, "uri_template", "")
                    ),
                    "name": getattr(t, "name", None),
                }
                for t in templates_raw
            ]
            return {"count": len(templates), "resourceTemplates": templates}

        return await self._run_span(
            method="resources/templates/list",
            span_name="mcp:resources/templates/list",
            invoke=invoke,
            shape_request=req,
            shape_output=out,
        )

    async def list_prompts(self):
        async def invoke(meta):
            return await self.session.list_prompts(params=_page_params(meta))

        def req(meta):
            return {"method": "prompts/list", "params": {"_meta": meta}}

        def out(result):
            prompts = [
                {"name": p.name, "description": getattr(p, "description", None)}
                for p in (result.prompts or [])
            ]
            return {"count": len(prompts), "prompts": prompts}

        return await self._run_span(
            method="prompts/list",
            span_name="mcp:prompts/list",
            invoke=invoke,
            shape_request=req,
            shape_output=out,
        )

    async def get_prompt(self, name: str, arguments: dict | None = None):
        async def invoke(meta):
            # get_prompt may not take meta on all SDK versions
            try:
                return await self.session.get_prompt(name, arguments, meta=meta)
            except TypeError:
                return await self.session.get_prompt(name, arguments)

        return await self._run_span(
            method="prompts/get",
            span_name=f"mcp:prompts/get {name}",
            extra_meta={"mcp.prompt.name": name},
            invoke=invoke,
            shape_request=lambda meta: {
                "method": "prompts/get",
                "params": {"name": name, "arguments": arguments, "_meta": meta},
            },
            shape_output=lambda result: {
                "description": getattr(result, "description", None),
                "messages": [str(m) for m in (result.messages or [])],
            },
        )

    async def read_resource(self, uri: str):
        url = AnyUrl(uri)

        async def invoke(meta):
            try:
                return await self.session.read_resource(url, meta=meta or None)
            except TypeError:
                return await self.session.read_resource(url)

        def out(result):
            texts: list[Any] = []
            for block in getattr(result, "contents", None) or []:
                text = getattr(block, "text", None) or str(block)
                try:
                    texts.append(json.loads(text))
                except json.JSONDecodeError:
                    texts.append(text)
            return {"contents": texts if len(texts) != 1 else texts[0]}

        return await self._run_span(
            method="resources/read",
            span_name=f"mcp:resources/read {uri}",
            extra_meta={"mcp.resource.uri": uri},
            invoke=invoke,
            shape_request=lambda meta: {
                "method": "resources/read",
                "params": {"uri": uri, "_meta": meta},
            },
            shape_output=out,
        )

    async def call_tool(self, name: str, arguments: dict | None = None):
        arguments = arguments or {}

        async def invoke(meta):
            return await self.session.call_tool(name, arguments, meta=meta or None)

        return await self._run_span(
            method="tools/call",
            span_name=f"mcp:tools/call {name}",
            as_type="tool",
            extra_meta={"mcp.tool.name": name},
            invoke=invoke,
            shape_request=lambda meta: {
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": arguments,
                    "_meta": meta,
                },
            },
            shape_output=_result_payload,
        )

    async def list_agents(self):
        async def invoke(meta):
            return await self.session.list_agents(params=_page_params(meta))

        def req(meta):
            return {"method": "agents/list", "params": {"_meta": meta}}

        def out(result):
            agents = [
                {
                    "name": a.name,
                    "description": a.description,
                    "capabilities": list(a.capabilities or []),
                }
                for a in result.agents
            ]
            return {"count": len(agents), "agents": agents}

        return await self._run_span(
            method="agents/list",
            span_name="mcp:agents/list",
            invoke=invoke,
            shape_request=req,
            shape_output=out,
        )

    async def get_agent(self, name: str):
        async def invoke(meta):
            return await self.session.get_agent(name, meta=meta)

        def req(meta):
            return {
                "method": "agents/get",
                "params": {"name": name, "_meta": meta},
            }

        def out(result):
            tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": getattr(t, "inputSchema", None)
                    or getattr(t, "input_schema", None),
                }
                for t in result.tools
            ]
            return {
                "agent": result.agent,
                "instructions": result.instructions,
                "tool_count": len(tools),
                "tools": tools,
            }

        return await self._run_span(
            method="agents/get",
            span_name=f"mcp:agents/get {name}",
            extra_meta={"mcp.agent.name": name},
            invoke=invoke,
            shape_request=req,
            shape_output=out,
        )

    async def run_agent_first_discovery(self) -> dict[str, Any]:
        """Proposal 1 path: initialize → agents/list (no flat tools/list)."""
        init = await self.initialize()
        server_info = getattr(init, "serverInfo", None) or getattr(init, "server_info", None)
        name = getattr(server_info, "name", None) if server_info else None
        print(
            f"mcp:initialize → {name} protocol={getattr(init, 'protocolVersion', None) or getattr(init, 'protocol_version', None)}"
        )

        caps = getattr(init, "capabilities", None)
        experimental = getattr(caps, "experimental", None) or {}
        if "agents" not in experimental:
            raise RuntimeError(
                "Server did not advertise capabilities.experimental.agents; "
                "cannot run agent-first discovery"
            )
        print("mcp:initialize → capabilities.experimental.agents present")

        await self.send_ping()
        print("mcp:ping → ok")

        roster = await self.list_agents()
        print(f"mcp:agents/list → {len(roster.agents)} agents (roster only, no tool schemas)")
        for a in roster.agents:
            print(f"  - {a.name}: {a.description}")

        return {"init": init, "agents": roster, "mode": "agent-first"}

    async def run_default_discovery(self) -> dict[str, Any]:
        init = await self.initialize()
        server_info = getattr(init, "serverInfo", None) or getattr(init, "server_info", None)
        name = getattr(server_info, "name", None) if server_info else None
        print(
            f"mcp:initialize → {name} protocol={getattr(init, 'protocolVersion', None) or getattr(init, 'protocol_version', None)}"
        )

        caps = getattr(init, "capabilities", None)
        experimental = getattr(caps, "experimental", None) or {}
        if "agents" in experimental:
            # Prefer agent-first when the server advertises it
            await self.send_ping()
            print("mcp:ping → ok")
            roster = await self.list_agents()
            print(f"mcp:agents/list → {len(roster.agents)} agents")
            return {
                "init": init,
                "agents": roster,
                "mode": "agent-first",
                "tools": None,
                "resources": None,
                "templates": None,
                "prompts": None,
            }

        await self.send_ping()
        print("mcp:ping → ok")

        listed = await self.list_tools()
        print(f"mcp:tools/list → {len(listed.tools)} tools")

        resources = await self.list_resources()
        print(f"mcp:resources/list → {len(resources.resources or [])} resources")

        templates = await self.list_resource_templates()
        templates_list = getattr(templates, "resourceTemplates", None) or getattr(
            templates, "resource_templates", None
        ) or []
        print(f"mcp:resources/templates/list → {len(templates_list)} templates")

        prompts = await self.list_prompts()
        print(f"mcp:prompts/list → {len(prompts.prompts or [])} prompts")

        return {
            "init": init,
            "tools": listed,
            "resources": resources,
            "templates": templates,
            "prompts": prompts,
            "mode": "flat-tools",
        }
