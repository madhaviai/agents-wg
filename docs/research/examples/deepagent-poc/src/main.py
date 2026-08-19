"""Terminal REPL — deep-agent supervisor (registry + task())."""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from src.run_helpers import invoke_agent, run_repl
from src.supervisor import build_supervisor

# Same release-readiness prompt used across deep / MCP-agents / MCP-flat demos
DEFAULT_QUESTION = (
    "We're shipping payments-service v2.4 tomorrow. Check failed pipelines and "
    "pending approvals, search for payment-API outages or CVEs, then give a "
    "release-risk insight with a chart."
)


def main() -> None:
    agent = build_supervisor()
    argv_q = " ".join(sys.argv[1:]).strip()
    if argv_q:
        # One-shot for WG demos / Langfuse traces
        question = DEFAULT_QUESTION if argv_q in {"--default", "-d"} else argv_q
        print(f"\nQuestion:\n{question}\n")
        invoke_agent(agent, question, mode="deep")
        return

    run_repl(
        agent,
        mode="deep",
        banner="Deep Agent — supervisor routes to workflow/research/insights via task()",
    )


if __name__ == "__main__":
    main()
