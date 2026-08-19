"""Research subagent — web search + internal docs for release risk context."""

from __future__ import annotations

from src.agent_registry import register_agent
from src.middleware.langfuse_tracing import LangfuseTracingMiddleware
from src.tools.catalog import RESEARCH_TOOLS

_SYSTEM_PROMPT = """You are the research specialist for SDLC release readiness.

Search the web and internal docs for outages, CVEs, and runbook guidance related
to the user's release question. Cite URLs. Keep summaries short.
"""

register_agent(
    name="research-agent",
    description=(
        "External + internal research: web search for outages/CVEs, URL summaries, "
        "and internal release runbook lookups."
    ),
    capabilities=["web search", "URL summary", "runbooks"],
    example_tasks=[
        "Search for recent payment API outages or CVEs before release",
        "Find runbook guidance for checkout 503s",
    ],
    tools=RESEARCH_TOOLS,
    system_prompt=_SYSTEM_PROMPT,
    middleware=[
        LangfuseTracingMiddleware(agent_name="research-agent", agent_prompt=_SYSTEM_PROMPT),
    ],
)
