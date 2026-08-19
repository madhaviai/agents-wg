"""Insights subagent — DORA, error budget, charts, risk score."""

from __future__ import annotations

from src.agent_registry import register_agent
from src.middleware.langfuse_tracing import LangfuseTracingMiddleware
from src.tools.catalog import INSIGHTS_TOOLS

_SYSTEM_PROMPT = """You are the insights specialist for SDLC release readiness.

Pull DORA / error-budget / SLO burn data, compute a risk score, and build a chart
when asked. Always call build_insight_chart for visual release-risk output when
the user wants an insight graph. Report chart_path in your answer.
"""

register_agent(
    name="insights-agent",
    description=(
        "Release insights: DORA metrics, error budget, SLO burn, risk score, "
        "and insight chart (PNG) generation."
    ),
    capabilities=["DORA", "error budget", "SLO burn", "risk score", "charts"],
    example_tasks=[
        "Compute release risk and build an insight chart for payments-service",
        "What is the error budget remaining?",
    ],
    tools=INSIGHTS_TOOLS,
    system_prompt=_SYSTEM_PROMPT,
    middleware=[
        LangfuseTracingMiddleware(agent_name="insights-agent", agent_prompt=_SYSTEM_PROMPT),
    ],
)
