"""Workflow subagent — pipelines, approvals, change requests, deploy train."""

from __future__ import annotations

from src.agent_registry import register_agent
from src.middleware.langfuse_tracing import LangfuseTracingMiddleware
from src.tools.catalog import WORKFLOW_TOOLS

_SYSTEM_PROMPT = """You are the workflow / CI-CD specialist for SDLC release readiness.

Use your tools to inspect failed pipelines, pending approvals, change requests,
deployments, incidents, flags, and flakes. Be concise and factual. Prefer JSON
facts from tools over speculation.
"""

register_agent(
    name="workflow-agent",
    description=(
        "CI/CD and release train: failed pipelines, approval gates, change requests, "
        "deployments, incidents, feature flags, flaky tests, service owners."
    ),
    capabilities=[
        "failed pipelines",
        "approvals",
        "change requests",
        "deployments",
        "incidents",
    ],
    example_tasks=[
        "Show failed pipelines and pending approvals for payments-service",
        "List open change requests blocking the v2.4 release",
    ],
    tools=WORKFLOW_TOOLS,
    system_prompt=_SYSTEM_PROMPT,
    middleware=[
        LangfuseTracingMiddleware(agent_name="workflow-agent", agent_prompt=_SYSTEM_PROMPT),
    ],
)
