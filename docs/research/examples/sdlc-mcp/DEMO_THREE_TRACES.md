# WG live demo — three traces, same prompt

**Prompt (all three runs):**

```text
We're shipping payments-service v2.4 tomorrow. Check failed pipelines and
pending approvals, search for payment-API outages or CVEs, then give a
release-risk insight with a chart.
```

(`--default` uses this text.)

**Before you start:** Langfuse at `http://localhost:3000`, keys in each project `.env`, Groq/OpenAI set.

---

## 1) Deep Agent — 3 `task()` handoffs

```bash
cd /Users/madhavik/my_workspace/mcp-agents-poc/deepagent-poc
source .venv/bin/activate
python -m src.main --default
```

**Look for in Langfuse:** tag `mode:deep` — nested `task(workflow)`, `task(research)`, `task(insights)`.

---

## 2) MCP **with** agents (Proposal 1)

Starts `server.main` → calls `register_agents()` → advertises `experimental.agents`.

```bash
cd /Users/madhavik/my_workspace/mcp-agents-poc/sdlc-mcp
source .venv/bin/activate
python -m client.main --default
```

**Look for:** `mcp:agents/list` → `mcp:agents/get …` → `mcp:tools/call …`  
**No** full `tools/list` into the supervisor. Tag: `agent-first`.

---

## 3) MCP **without** agents (flat — old path)

Starts `server.main_flat` → **same tools**, **no** `register_agents()` → no agents capability → client uses `tools/list`.

```bash
cd /Users/madhavik/my_workspace/mcp-agents-poc/sdlc-mcp
source .venv/bin/activate
python -m client.main --flat --default
```

**Look for:** `mcp:tools/list` (~18 tools) → model picks tools itself. Tag: `mode:mcp-flat`.

---

## Cheat sheet

| Demo | Command | Server module | Discovery |
|------|---------|---------------|-----------|
| Deep | `python -m src.main --default` | (in-process) | registry + `task()` |
| MCP agents | `python -m client.main --default` | `server.main` | `agents/list` → `get` |
| MCP flat | `python -m client.main --flat --default` | `server.main_flat` | `tools/list` |

No env toggle: **with agents = register agents; without = don’t register.**
