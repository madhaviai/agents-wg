# SDLC MCP — agent-first discovery

Same release-readiness tools as [`../deepagent-poc`](../deepagent-poc). Default
entry is **agent-first** (`agents/list` → `agents/get` → `tools/call`). `server/main_flat.py`
is the comparison path: **all tools** on `tools/list`.

**End-to-end (Helm Langfuse, both apps, three traces):** **[../README.md](../README.md)**.

```text
deepagent-poc                          sdlc-mcp (this project)
─────────────                          ───────────────────────
Supervisor sees agent roster           Client sees ~18 tool schemas
task(workflow|research|insights)       tools/call(name, args)
Specialist picks tools                 Client/LLM picks tools
```

## Call sequence (learn this)

```text
Client                         Server                         Backend
──────                         ──────                         ───────
initialize  ─────────────────► hello
tools/list  ─────────────────► return [{name, description, inputSchema}, ...]
tools/call  ─────────────────► run @mcp.tool function ──────► fixture / HTTP API
  (+ _meta.traceparent)          (continue same OTEL trace)
            ◄───────────────── CallToolResult
```

- **`tools/list`** — discovery only. No APIs called yet.
- **`tools/call`** — SDK invokes your Python function; **you** call fixtures/APIs inside it.
- **`params._meta`** — [SEP-414](https://modelcontextprotocol.io/seps/414-request-meta): carry
  W3C `traceparent` so client + server spans join one Langfuse tree.

### Langfuse — production-like MCP tracing (SEP-414)

```bash
# .env
LANGFUSE_ENABLED=true
MCP_TRACING_MODE=production   # client + nested server (default)
# MCP_TRACING_MODE=client     # caller-only wire spans

python -m client.main "Show pending approvals for payments-service"
```

One tree per turn:

```text
mcp-session
├── mcp:initialize
├── mcp:ping
├── mcp:tools/list
│     └── mcp.server:tools/list          ← executor (same trace_id via _meta)
├── mcp:resources/list
│     └── mcp.server:resources/list
├── mcp:resources/read …
│     └── mcp.server:resources/read …
├── mcp:tools/call list_pending_approvals
│     └── mcp.server:tools/call …        ← parent = client tools/call span
└── …
```

- **Caller (client)** records each JSON-RPC request + result.
- **Executor (server)** nests only when ``params._meta.traceparent`` / langfuse ids
  are present — never opens a new root (no orphan traces).
- Langfuse SDK is OTEL-backed; MCP SDK itself does not push telemetry.

## Setup

```bash
cd docs/research/examples/sdlc-mcp
python3 -m venv .venv
source .venv/bin/activate
# Experimental SDK with agents/list + agents/get (path from this folder):
pip install -e ../../../../../python-sdk[cli]
pip install -e ../../../../../python-sdk/src/mcp-types
pip install -r requirements.txt
cp .env.example .env   # fill Groq/OpenAI; Langfuse optional
```

Optional: `EXA_API_KEY` or `TAVILY_API_KEY` for live `web_search` (else fixtures).

## Run the learning client

```bash
python -m client.main
```

You should see `initialize`, then all tool names from `tools/list`, then a sample
`tools/call` for `list_pending_approvals`.

## Run the server alone (stdio)

```bash
python -m server.main
```

Or with MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python -m server.main
```

## Tool inventory (same as deepagent subagents)

| Group (deepagent) | Tools |
|-------------------|--------|
| workflow-agent | `list_failed_pipelines`, `list_pending_approvals`, `get_pipeline_run`, `list_open_change_requests`, `list_deployments`, `get_service_owners`, `list_open_incidents`, `list_feature_flags`, `list_test_flakes`, `get_task_config` |
| research-agent | `web_search`, `fetch_url_summary`, `search_internal_docs` |
| insights-agent | `get_dora_metrics`, `get_error_budget`, `get_slo_burn`, `build_insight_chart`, `compute_release_risk_score` |

Grouping above is **deepagent-only** (supervisor + `task()`). Flat MCP exposes all
tools on `tools/list` with no routing resource — the client LLM picks tools from
schemas. A future `agents/list` would be the scalable MCP analog of the registry.

## Layout

```text
sdlc-mcp/
├── requirements.txt
├── server/
│   ├── main.py       # FastMCP + @mcp.tool registrations
│   ├── backend.py    # fixture / API implementations
│   ├── fixtures.py
│   └── sdlc.json
├── client/
│   └── main.py       # tools/list + tools/call demo
└── outputs/          # charts from build_insight_chart
```

## Cursor MCP config (optional)

```json
{
  "mcpServers": {
    "sdlc-release-readiness": {
      "command": "/Users/madhavik/my_workspace/mcp-agents-poc/sdlc-mcp/.venv/bin/python",
      "args": ["-m", "server.main"],
      "cwd": "/Users/madhavik/my_workspace/mcp-agents-poc/sdlc-mcp"
    }
  }
}
```
