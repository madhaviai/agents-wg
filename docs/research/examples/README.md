# Research examples — run both apps and see traces

Companions to [deep_agent_mcp_analysis.md](../deep_agent_mcp_analysis.md). Same SDLC
scenario (`payments-service` v2.4) two ways:

| App | Folder | What you are exercising |
| --- | ------ | ----------------------- |
| **Deep Agents** | [`deepagent-poc/`](./deepagent-poc) | Supervisor sees a **roster**, delegates with `task()`, specialists own tools |
| **MCP Agents** | [`sdlc-mcp/`](./sdlc-mcp) | Same tools on the wire: **`agents/list` → `agents/get` → `tools/call`** (optional flat `tools/list`) |

Traces go to **self-hosted Langfuse** (Helm + port-forward). Skip Helm only if you already
have Langfuse at `http://localhost:3000` (or Langfuse Cloud).

**Shared prompt (all runs):**

```text
We're shipping payments-service v2.4 tomorrow. Check failed pipelines and
pending approvals, search for payment-API outages or CVEs, then give a
release-risk insight with a chart.
```

---

## 0. Prerequisites

- Python 3.11+
- A Groq (or OpenAI) API key
- Kubernetes + Helm (minikube, kind, Docker Desktop, or any cluster)
- This repo layout (experimental SDK lives next to `agents-wg`):

```text
mcp-agents-poc/
├── python-sdk/          # experimental agents/list + agents/get
└── agents-wg/docs/research/examples/
    ├── deepagent-poc/
    └── sdlc-mcp/
```

From this `examples/` directory, paths below assume you `cd` into each app folder.

---

## 1. Install Langfuse with Helm

Official chart: [Kubernetes (Helm)](https://langfuse.com/self-hosting/deployment/kubernetes-helm).
Bundled Postgres / ClickHouse / Redis / MinIO is enough for local demos.

```bash
# cluster must be up: kubectl get nodes
helm repo add langfuse https://langfuse.github.io/langfuse-k8s
helm repo update

kubectl create namespace langfuse

helm install langfuse langfuse/langfuse -n langfuse
```

Wait until pods are Running (web/worker may restart while databases come up — can take ~5 min):

```bash
kubectl get pods -n langfuse -w
```

Expose the UI on localhost:

```bash
kubectl port-forward svc/langfuse-web -n langfuse 3000:3000
```

Leave that terminal open. Open [http://localhost:3000](http://localhost:3000).

### Create a project and API keys

1. Register / sign in.
2. Create an **organization** and a **project** (e.g. `mcp-agents-poc`).
3. Project → **Settings → API keys** → create keys.
4. Copy **public** (`pk-lf-…`) and **secret** (`sk-lf-…`).

Teardown later:

```bash
helm uninstall langfuse -n langfuse
kubectl delete namespace langfuse
```

Cloud instead of Helm: set `LANGFUSE_HOST=https://cloud.langfuse.com` and use cloud keys.

---

## 2. Configure `.env` in both apps

Copy examples and paste the same Langfuse keys + your LLM key into **both** folders.

```bash
# from docs/research/examples
cp deepagent-poc/.env.example deepagent-poc/.env
cp sdlc-mcp/.env.example sdlc-mcp/.env
```

Minimum in each `.env`:

```bash
GROQ_API_KEY=gsk_...                 # or OPENAI_API_KEY=sk-...
MODEL=llama-3.3-70b-versatile

LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3000

# sdlc-mcp only — nest server spans under the client (SEP-414)
MCP_TRACING_MODE=production
```

Keep `kubectl port-forward` running whenever you want traces to land.

---

## 3. Install and run Deep Agents (`deepagent-poc`)

```bash
cd deepagent-poc
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m src.main --default
```

Interactive instead of `--default`:

```bash
python -m src.main
```

**What changed vs a flat tool dump:** supervisor context is three agent cards, not ~18
schemas. Specialists call tools after `task(workflow|research|insights)`.

**In Langfuse:** filter tag `mode:deep`. Expect nested handoffs
`task(workflow)` → `task(research)` → `task(insights)`, then synthesize.

Charts (if generated) land under `outputs/`.

---

## 4. Install and run MCP Agents (`sdlc-mcp`)

Needs the **experimental Python SDK** in this workspace (not on PyPI).

```bash
cd sdlc-mcp
python3 -m venv .venv
source .venv/bin/activate

# from sdlc-mcp/ — five levels up to mcp-agents-poc/python-sdk
pip install -e ../../../../../python-sdk[cli]
pip install -e ../../../../../python-sdk/src/mcp-types
pip install -r requirements.txt
```

### 4a. Agent-first (Proposal 1 — the change)

Client starts `server.main`, which **registers agents**. Server advertises
`experimental.agents`. Client does **not** dump `tools/list` into the supervisor.

```bash
python -m client.main --default
```

**In Langfuse:** tag `agent-first`. Look for:

```text
mcp:initialize          → experimental.agents
mcp:agents/list         → roster cards only
mcp:agents/get …        → scoped tool schemas
mcp:tools/call …        → same-server ToolManager
  └── mcp.server:tools/call …   (nested if MCP_TRACING_MODE=production)
```

### 4b. Flat tools (old path — comparison)

Same tools, **no** agent registration (`server.main_flat`). Client uses `tools/list`.

```bash
python -m client.main --flat --default
```

**In Langfuse:** tag `mode:mcp-flat`. Look for `mcp:tools/list` (~18 schemas) then
`tools/call` — the model picks tools itself.

### Server only / Inspector (optional)

```bash
python -m server.main          # agent-first stdio server
# python -m server.main_flat   # flat tools/list server
npx @modelcontextprotocol/inspector python -m server.main
```

---

## 5. Compare the three traces

Use the **same prompt** (`--default`) for all three. In Langfuse, open the project and
sort by time (or filter tags).

| # | Command | Discovery | Langfuse tag |
| - | ------- | --------- | ------------ |
| 1 | `cd deepagent-poc && python -m src.main --default` | in-process roster + `task()` | `mode:deep` |
| 2 | `cd sdlc-mcp && python -m client.main --default` | `agents/list` → `agents/get` | `agent-first` |
| 3 | `cd sdlc-mcp && python -m client.main --flat --default` | `tools/list` | `mode:mcp-flat` |

What to compare: routing (who vs which tool), token/context size on the supervisor,
latency, and whether schemas appear only after agent selection.

---

## 6. Troubleshooting

| Symptom | What to check |
| ------- | ------------- |
| No traces | `LANGFUSE_ENABLED=true`, keys match the project, `LANGFUSE_HOST=http://localhost:3000`, port-forward still running |
| Helm pods CrashLoop | wait for Postgres/ClickHouse; `kubectl logs -n langfuse deploy/langfuse-web` |
| `agents/list` unknown method | experimental `python-sdk` not installed editable; re-run the two `pip install -e` lines |
| Groq `tool_use_failed` | client already forces one tool per turn; retry; confirm `GROQ_API_KEY` and `MODEL` |
| Empty web search | expected without `EXA_API_KEY` / `TAVILY_API_KEY` (fixtures) |

Per-app detail: [deepagent-poc/README.md](./deepagent-poc/README.md), [sdlc-mcp/README.md](./sdlc-mcp/README.md).
