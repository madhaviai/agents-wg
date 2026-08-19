"""MCP client — nested Langfuse MCP spans + Groq-safe sequential tool calling.

Groq often fails when the model emits many tools in one shot (`tool_use_failed`).
We disable parallel tool calls and instruct the model to call ONE tool per turn.

  cd /Users/madhavik/my_workspace/mcp-agents-poc/sdlc-mcp
  source .venv/bin/activate
  python -m client.main
  python -m client.main --flat
  python -m client.main --default
  python -m client.main --flat --default
  python -m client.main --flat "Show pending approvals for payments-service"
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_ROOT / ".env")
load_dotenv(_ROOT.parent / "deepagent-poc" / ".env", override=False)

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.langfuse_client import (
    child_server_env,
    flush,
    get_langfuse,
    langfuse_enabled,
    langfuse_server_enabled,
    tracing_mode,
)
from shared.mcp_trace import TracedMcpSession
from client.agent_tracing import AgentLlmTracingMiddleware

DEFAULT_QUESTION = (
    "We're shipping payments-service v2.4 tomorrow. Check failed pipelines and "
    "pending approvals, search for payment-API outages or CVEs, then give a "
    "release-risk insight with a chart."
)

_SYSTEM = """You answer SDLC release-readiness questions using MCP tools.

Rules for tool calling (required for Groq):
1. Call exactly ONE tool per assistant turn.
2. Wait for that tool's result before calling another tool.
3. Never emit multiple tools in one response.
4. Use native tool calling only — never invent XML like <function=...>.
5. Be concise and factual after you have enough tool results.
"""


def _build_llm():
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if groq_key:
        from langchain_groq import ChatGroq

        model = os.getenv("MODEL", "llama-3.3-70b-versatile").strip()
        for prefix in ("openai:", "groq:", "langchain_groq:"):
            if model.startswith(prefix):
                model = model[len(prefix) :]
                break
        # Critical: Groq 400 tool_use_failed when the model batches many tools
        return ChatGroq(
            api_key=groq_key,
            model=model,
            temperature=0,
            model_kwargs={"parallel_tool_calls": False},
        )

    from langchain.chat_models import init_chat_model

    return init_chat_model(os.getenv("MODEL", "openai:gpt-4o-mini"), temperature=0)


def _tools_from_mcp(traced: TracedMcpSession, listed) -> list:
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field, create_model

    tools = []
    for t in listed.tools:
        schema = (
            getattr(t, "inputSchema", None)
            or getattr(t, "input_schema", None)
            or {"type": "object", "properties": {}}
        )
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])

        fields: dict[str, Any] = {}
        for key, prop in props.items():
            typ: type = str
            ptype = (prop or {}).get("type")
            if ptype == "integer":
                typ = int
            elif ptype == "number":
                typ = float
            elif ptype == "boolean":
                typ = bool
            default = ... if key in required else (prop or {}).get("default", None)
            desc = (prop or {}).get("description", "")
            if default is ...:
                fields[key] = (typ, Field(description=desc))
            else:
                fields[key] = (typ | None, Field(default=default, description=desc))

        ArgModel = (
            create_model(f"{t.name}_Args", **fields)
            if fields
            else create_model(f"{t.name}_Args")
        )

        def _bind(tool_name: str, description: str, arg_model: type[BaseModel]):
            async def _call(**kwargs: Any) -> str:
                args = {k: v for k, v in kwargs.items() if v is not None}
                result = await traced.call_tool(tool_name, args)
                parts = []
                for block in result.content or []:
                    parts.append(getattr(block, "text", None) or str(block))
                return "\n".join(parts)

            return StructuredTool.from_function(
                coroutine=_call,
                name=tool_name,
                description=description or tool_name,
                args_schema=arg_model,
            )

        tools.append(_bind(t.name, t.description or t.name, ArgModel))
    return tools


def _is_groq_tool_fail(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "tool_use_failed" in text or "failed to call a function" in text


class _ToolListing:
    """Adapter so _tools_from_mcp works with agents/get results."""

    def __init__(self, tools):
        self.tools = tools


async def _select_agents(question: str, roster, *, model) -> list[str]:
    """Supervisor sees roster cards only — pick one or more agents by name."""
    cards = "\n".join(
        f"- {a.name}: {a.description} | capabilities={list(a.capabilities or [])}"
        for a in roster.agents
    )
    prompt = (
        "You are a supervisor. Pick which specialist agent(s) should handle the user question.\n"
        "Reply with a comma-separated list of agent names only (no other text).\n\n"
        f"Agents:\n{cards}\n\n"
        f"User question: {question}"
    )
    from langchain_core.messages import HumanMessage

    resp = await model.ainvoke([HumanMessage(content=prompt)])
    text = str(getattr(resp, "content", None) or resp).strip()
    known = {a.name for a in roster.agents}
    picked = []
    for token in text.replace("\n", ",").split(","):
        name = token.strip().strip("`").strip()
        if name in known and name not in picked:
            picked.append(name)
    if not picked:
        # Fallback: workflow for approvals/pipelines, else all
        q = question.lower()
        if "cve" in q or "outage" in q or "search" in q:
            picked.append("research-agent")
        if "dora" in q or "risk" in q or "chart" in q or "insight" in q:
            picked.append("insights-agent")
        if "pipeline" in q or "approval" in q or "deploy" in q or not picked:
            picked.insert(0, "workflow-agent")
    return picked


async def ask(question: str, *, traced: TracedMcpSession, lf, discovery: dict) -> str:
    model = _build_llm()
    mode = discovery.get("mode") or "flat-tools"

    if mode == "agent-first":
        roster = discovery["agents"]
        selected = await _select_agents(question, roster, model=model)
        print(f"supervisor selected agents: {selected}")

        scoped_tools = []
        instructions_bits = []
        for name in selected:
            detail = await traced.get_agent(name)
            print(
                f"mcp:agents/get {name} → {len(detail.tools)} tools "
                f"({', '.join(t.name for t in detail.tools)})"
            )
            scoped_tools.extend(detail.tools)
            if detail.instructions:
                instructions_bits.append(f"[{name}] {detail.instructions}")

        # Dedupe by tool name (keep first)
        seen: set[str] = set()
        unique = []
        for t in scoped_tools:
            if t.name in seen:
                continue
            seen.add(t.name)
            unique.append(t)

        listed = _ToolListing(unique)
        system = _SYSTEM
        if instructions_bits:
            system = _SYSTEM + "\n\nSpecialist notes:\n" + "\n".join(instructions_bits)
    else:
        listed = discovery["tools"]
        system = _SYSTEM

    lc_tools = _tools_from_mcp(traced, listed)
    print(f"tool schemas loaded into LLM: {len(lc_tools)} (mode={mode})")

    from langchain.agents import create_agent

    middleware = [AgentLlmTracingMiddleware(lf=lf, agent_name="mcp-agent")] if lf else []

    agent = create_agent(
        model=model,
        tools=lc_tools,
        system_prompt=system,
        middleware=middleware,
    )

    async def _invoke() -> str:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]}
        )
        messages = result.get("messages") or []
        last = messages[-1] if messages else None
        return str(getattr(last, "content", None) or last)

    try:
        return await _invoke()
    except Exception as exc:
        if not _is_groq_tool_fail(exc):
            raise
        print("[warn] Groq tool_use_failed — retrying with stricter one-tool-at-a-time nudge")
        nudge = (
            question
            + "\n\nIMPORTANT: Call only ONE tool in your next response "
            "(start with list_failed_pipelines), then stop and wait."
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": nudge}]}
        )
        messages = result.get("messages") or []
        last = messages[-1] if messages else None
        return str(getattr(last, "content", None) or last)


async def run_session(question: str, *, server_module: str = "server.main") -> str:
    lf = get_langfuse()
    print(f"langfuse: {'ON' if lf else 'OFF'} (enabled={langfuse_enabled()})")
    print(f"tracing mode: {tracing_mode()} (server spans={langfuse_server_enabled()})")
    print(f"server module: {server_module}")
    print(f"client path: {_ROOT / 'client'}")

    child_env = child_server_env()
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", server_module],
        cwd=str(_ROOT),
        env=child_env,
    )

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as raw:
            traced = TracedMcpSession(raw, lf)

            async def _run() -> str:
                discovery = await traced.run_default_discovery()
                return await ask(question, traced=traced, lf=lf, discovery=discovery)

            if lf is None:
                return await _run()

            discovery_holder: dict[str, Any] = {}

            async def _run_tagged() -> str:
                discovery = await traced.run_default_discovery()
                discovery_holder["d"] = discovery
                return await ask(question, traced=traced, lf=lf, discovery=discovery)

            # Tags refined after discovery (agent-first iff server advertised agents)
            with lf.start_as_current_observation(
                name="mcp-session",
                as_type="span",
                input=question,
                metadata={"mode": "mcp-prod"},
            ) as root:
                from langfuse import propagate_attributes

                with propagate_attributes(
                    user_id="poc-user",
                    session_id="sdlc-mcp-demo",
                    tags=["mode:mcp-prod", "sep-414", "scenario:release-readiness"],
                    trace_name="mcp-session",
                    metadata={"mode": "mcp-prod"},
                ):
                    answer = await _run_tagged()

                actual = (discovery_holder.get("d") or {}).get("mode") or "flat-tools"
                tags = ["mode:mcp-prod", "sep-414", "scenario:release-readiness"]
                if actual == "agent-first":
                    tags.append("agent-first")
                    trace_name = "mcp-session-agent-first"
                else:
                    tags.append("mode:mcp-flat")
                    trace_name = "mcp-session-flat"
                try:
                    lf.update_current_trace(name=trace_name, tags=tags)
                except Exception:  # noqa: BLE001
                    pass
                root.update(output=str(answer)[:4000])
                try:
                    tid = lf.get_current_trace_id()
                    url = lf.get_trace_url()
                    print(f"\n[langfuse] trace_id={tid}")
                    if url:
                        print(f"[langfuse] url={url}")
                except Exception:  # noqa: BLE001
                    pass
                return answer


async def interactive(*, server_module: str = "server.main") -> None:
    flat = server_module.endswith("main_flat")
    print("MCP client — production SEP-414 tracing (client + nested server).")
    print(f"Using: {_ROOT}")
    print(f"server: {server_module}")
    print(f"Mode: {'FLAT tools/list' if flat else 'AGENT-FIRST agents/list'}")
    print(f"tracing mode: {tracing_mode()} (server spans={langfuse_server_enabled()})")
    print(f"Try: {DEFAULT_QUESTION}\n")

    lf = get_langfuse()
    child_env = child_server_env()
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", server_module],
        cwd=str(_ROOT),
        env=child_env,
    )

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as raw:
            traced = TracedMcpSession(raw, lf)

            if lf is None:
                discovery = await traced.run_default_discovery()
            else:
                with lf.start_as_current_observation(
                    name="mcp-session:connect",
                    as_type="span",
                    input="MCP discovery",
                    metadata={"mode": "mcp-prod"},
                ) as connect:
                    try:
                        lf.update_current_trace(name="mcp-session:connect")
                    except Exception:  # noqa: BLE001
                        pass
                    discovery = await traced.run_default_discovery()
                    connect.update(
                        output={
                            "mode": discovery.get("mode"),
                            "agents": len(getattr(discovery.get("agents"), "agents", []) or [])
                            if discovery.get("agents")
                            else None,
                            "tools": len(discovery["tools"].tools)
                            if discovery.get("tools")
                            else None,
                        }
                    )
                    try:
                        print(f"[langfuse] connect {lf.get_trace_url()}")
                    except Exception:  # noqa: BLE001
                        pass
                    flush()

            listed = discovery  # full discovery dict for agent-first
            print("Ready.\n")

            while True:
                try:
                    user = input("You> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not user:
                    continue
                if user.lower() in {"exit", "quit", "q"}:
                    break

                try:
                    if lf is None:
                        answer = await ask(
                            user, traced=traced, lf=lf, discovery=listed
                        )
                    else:
                        with lf.start_as_current_observation(
                            name="mcp-session",
                            as_type="span",
                            input=user,
                            metadata={"mode": "mcp-prod"},
                        ) as root:
                            try:
                                lf.update_current_trace(
                                    name="mcp-session",
                                    user_id="poc-user",
                                    session_id="sdlc-mcp-demo",
                                    tags=["mode:mcp-prod", "sep-414", "agent-first"],
                                    metadata={"mode": "mcp-prod"},
                                )
                            except Exception:  # noqa: BLE001
                                pass
                            answer = await ask(
                                user, traced=traced, lf=lf, discovery=listed
                            )
                            root.update(output=str(answer)[:4000])
                            try:
                                lf.update_current_trace(name="mcp-session")
                            except Exception:  # noqa: BLE001
                                pass
                            try:
                                print(f"[langfuse] {lf.get_trace_url()}")
                            except Exception:  # noqa: BLE001
                                pass
                    print(f"\nAssistant:\n{answer}\n")
                except Exception as exc:  # noqa: BLE001
                    print(f"\n[error] {exc}\n")
                finally:
                    flush()


async def main() -> None:
    args = [a for a in sys.argv[1:] if a]
    flat = "--flat" in args
    args = [a for a in args if a != "--flat" and a != "--"]

    server_module = "server.main_flat" if flat else "server.main"
    argv_q = " ".join(args).strip()

    # Only --default / -d forces the canned release-readiness prompt.
    if argv_q in {"--default", "-d"}:
        argv_q = DEFAULT_QUESTION
    elif not argv_q:
        # No question → type at the You> prompt (works with or without --flat)
        await interactive(server_module=server_module)
        return

    print(f"\nQuestion:\n{argv_q}\n")
    print(f"Mode: {'FLAT tools/list' if flat else 'AGENT-FIRST agents/list'}\n")
    answer = await run_session(argv_q, server_module=server_module)
    print(f"\nAnswer:\n{answer}\n")
    flush()


if __name__ == "__main__":
    asyncio.run(main())
