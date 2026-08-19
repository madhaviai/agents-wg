"""Supervisor — registry + task() handoffs (lean, Groq-friendly).

Uses LangChain ``create_agent`` instead of ``create_deep_agent`` so Groq models
are not flooded with filesystem/todo tools that break tool-call validation.
The routing pattern is the same: supervisor sees name+description only, then
``task(subagent_type, description)`` runs the specialist.
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field

from src.agent_registry import build_delegation_block, get_all_agent_configs
from src.llm import build_chat_model
from src.middleware.langfuse_tracing import LangfuseTracingMiddleware


def _import_agents() -> None:
    import src.subagents.insights  # noqa: F401
    import src.subagents.research  # noqa: F401
    import src.subagents.workflow  # noqa: F401


def _build_subagent_runnables(model):
    """Compile each registered specialist into its own agent graph."""
    """subagent === agent"""
    runnables = {}
    for cfg in get_all_agent_configs():
        name = cfg["name"]
        mw = list(cfg.get("middleware") or [])
        if not mw:
            mw = [
                LangfuseTracingMiddleware(
                    agent_name=name,
                    agent_prompt=cfg.get("system_prompt"),
                )
            ]
        runnables[name] = create_agent(
            model=model,
            tools=cfg.get("tools") or [],
            system_prompt=cfg.get("system_prompt") or f"You are {name}.",
            name=name,
            middleware=mw,
        )
    return runnables


def build_supervisor():
    _import_agents()
    model = build_chat_model(temperature=0)
    sub_runnables = _build_subagent_runnables(model)
    allowed = sorted(sub_runnables.keys())
    allowed_set = set(allowed)
    delegation = build_delegation_block()

    class TaskInput(BaseModel):
        subagent_type: str = Field(
            description=f"Specialist name. Exactly one of: {', '.join(allowed)}"
        )
        description: str = Field(
            description="Clear task for the specialist (include service name when relevant)."
        )

    def _task_impl(subagent_type: str, description: str) -> str:
        if subagent_type not in allowed_set:
            return f"Unknown subagent_type={subagent_type!r}. Allowed: {allowed}"
        agent = sub_runnables[subagent_type]

        # Share parent Langfuse context so specialist spans nest under handoff
        from langgraph.config import get_config
        from src.middleware.langfuse_tracing import _force_parent_oid_var, _trace_id_var

        parent_cfg: dict = {}
        try:
            cfg = get_config() or {}
            parent_cfg = dict((cfg.get("configurable") or {}))
        except Exception:  # noqa: BLE001
            parent_cfg = {}
        if not parent_cfg.get("trace_id"):
            tid = _trace_id_var.get(None)
            if tid:
                parent_cfg["trace_id"] = tid
        forced = _force_parent_oid_var.get(None)
        if forced:
            parent_cfg["parent_observation_id"] = forced
        parent_cfg.setdefault("mode", "deep")
        parent_cfg.setdefault("tags", ["mode:deep", "scenario:release-readiness"])

        result = agent.invoke(
            {"messages": [{"role": "user", "content": description}]},
            config={"configurable": parent_cfg},
        )
        messages = result.get("messages") or []
        if not messages:
            return "(subagent returned no messages)"
        last = messages[-1]
        return str(getattr(last, "content", None) or last)

    task_tool = StructuredTool.from_function(
        func=_task_impl,
        name="task",
        description=(
            "Delegate work to a registered specialist. "
            "Pass subagent_type (exact name) and description. "
            "Do not call domain tools yourself."
        ),
        args_schema=TaskInput,
    )

    system_prompt = f"""You are the supervisor orchestrator for SDLC release readiness.

You do NOT call domain tools yourself. You ONLY use the task tool to route.

{delegation}

For a full release-readiness question, call task multiple times as needed:
1. workflow-agent — pipelines, approvals, CRs, incidents
2. research-agent — web/CVE/outage context
3. insights-agent — risk score + insight chart

Rules:
1. Always use native tool calling for task with fields subagent_type and description.
2. After specialists return, synthesize a short go/hold with evidence.
3. Mention chart_path if insights-agent produced a chart.
"""

    return create_agent(
        model=model,
        tools=[task_tool],
        system_prompt=system_prompt,
        checkpointer=MemorySaver(),
        name="supervisor",
        middleware=[
            LangfuseTracingMiddleware(agent_name="supervisor", agent_prompt=system_prompt),
        ],
    )
