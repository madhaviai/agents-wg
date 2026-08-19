# Deep Agent POC — SDLC Release Readiness

Deep-agent supervisor with **registry + `task()` handoffs**. End-to-end install, Helm
Langfuse, and how to compare traces: **[../README.md](../README.md)**.

For the **MCP** comparison (agent-first or flat `tools/list`), use [`../sdlc-mcp`](../sdlc-mcp).

## Scenario

**Release readiness for `payments-service` v2.4**

> We're shipping payments-service v2.4 tomorrow. Check failed pipelines and
> pending approvals, search the web for recent payment-API outages or CVE notes,
> then give a release-risk insight with a simple chart.

```text
You → Supervisor (registry only)
         → task(workflow | research | insights)
              → specialist tools
         → synthesize go/hold + chart
         ↓
    Langfuse (mode:deep)  ↔  compare with sdlc-mcp (mode:mcp-flat)
```

## Setup

```bash
cd docs/research/examples/deepagent-poc
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Groq/OpenAI + Langfuse (+ optional EXA_API_KEY)
```

### Langfuse (self-hosted)

```bash
kubectl port-forward svc/langfuse-web 3000:3000
```

### Optional live web search

Set `EXA_API_KEY` (or `TAVILY_API_KEY`). Without them, `web_search` uses fixtures.

## Run

```bash
python -m src.main
```

Charts are written under `outputs/*_release_risk.png`.

## Deep-agent flow (what to demo)

Lean supervisor (registry + `task()` only — no deepagents filesystem/todo tools,
so Groq tool-calling stays valid). Same mental model as production deep agents.

```text
You: "payments-service v2.4 release readiness…"
        │
        ▼
 ┌─ supervisor:llm ─────────────────────────────┐
 │  Sees ONLY registry cards (name+description) │
 │  Decides: task(workflow) / task(research) /  │
 │           task(insights)                     │
 └──────────────┬───────────────────────────────┘
                │ handoff spans (numbered)
     ┌──────────┼──────────┐
     ▼          ▼          ▼
 workflow    research   insights
 (10 tools)  (Exa/web)  (chart/DORA)
     │          │          │
     └──────────┴──────────┘
                ▼
 ┌─ supervisor:synthesize ──┐
 │  go/hold + chart_path    │
 └──────────────────────────┘
```

Langfuse timeline names:

| Span | Meaning |
|------|---------|
| `00.supervisor:route` | Supervisor chose `task(...)` targets |
| `01.handoff→workflow-agent` | Full subagent run (nested llm/tool spans) |
| `02.handoff→research-agent` | Web/CVE research |
| `03.handoff→insights-agent` | Risk score + chart |
| `98.supervisor:synthesize` | Final answer |
| `99.supervisor:final` | Flush + `route_summary` |

Console also prints: `[route] …` / `[handoff] 01.handoff→…` / `[langfuse] route: …`

## Compare in Langfuse

1. Run deep: `python -m src.main` → tag `mode:deep`
2. Run flat MCP: `cd ../sdlc-mcp && python -m client` → tag `mode:mcp-flat`
3. Compare latency, input tokens, and tool-call shape

### Why `mode:deep` is usually slower

Sample (2026-07-17): deep full run **~5.2s** (`652771b4…`) vs mcp-flat narrow ask **~1.3s** (`292c3f7e…`).

Deep pays for supervisor routing + sequential handoffs (`workflow` → `research` → `insights`) + final synthesize — typically **~11 LLM generations** and more tools. Flat MCP is one agent with all tools; fewer LLM rounds when the prompt is smaller. Use the **same prompt** in both for an apples-to-apples latency compare.

## Layout

```text
src/
├── main.py
├── supervisor.py
├── agent_registry.py
├── run_helpers.py
├── middleware/langfuse_tracing.py
├── fixtures/sdlc.json
├── tools/catalog.py
└── subagents/{workflow,research,insights}.py
```

## Tool inventory (behind subagents)

| Group | Tools |
|-------|--------|
| Workflow (10) | failed pipelines, approvals, pipeline run, CRs, deployments, owners, incidents, flags, flakes, task config |
| Research (3) | web_search, fetch_url_summary, search_internal_docs |
| Insights (5) | DORA, error budget, SLO burn, build_insight_chart, risk score |
