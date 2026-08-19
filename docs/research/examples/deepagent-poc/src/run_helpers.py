"""Shared REPL / invoke helpers for the deep-agent supervisor."""

from __future__ import annotations

import os
import time
from typing import Any

from langfuse import Langfuse

from src.middleware.langfuse_tracing import begin_session, end_session


def langfuse_enabled() -> bool:
    return os.getenv("LANGFUSE_ENABLED", "false").lower() in ("1", "true", "yes")


def print_reply(result: dict) -> str:
    messages = result.get("messages") or []
    if not messages:
        print("(no messages)")
        return ""
    last = messages[-1]
    content = getattr(last, "content", None) or str(last)
    print("\nAssistant:\n" + content + "\n")
    return str(content)


def extract_usage_tokens(result: dict) -> dict[str, int]:
    """Best-effort token totals from AIMessage usage_metadata on the run."""
    prompt = 0
    completion = 0
    for msg in result.get("messages") or []:
        usage = getattr(msg, "usage_metadata", None) or {}
        if isinstance(usage, dict):
            prompt += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            completion += int(
                usage.get("output_tokens") or usage.get("completion_tokens") or 0
            )
        meta = getattr(msg, "response_metadata", None) or {}
        if isinstance(meta, dict):
            tu = meta.get("token_usage") or meta.get("usage") or {}
            if isinstance(tu, dict):
                prompt += int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0)
                completion += int(
                    tu.get("completion_tokens") or tu.get("output_tokens") or 0
                )
    return {"prompt_tokens": prompt, "completion_tokens": completion}


def invoke_agent(
    agent,
    user: str,
    *,
    mode: str,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Invoke agent with Langfuse session root; returns reply, latency, tokens, trace_id."""
    lf_on = langfuse_enabled()
    trace_id = Langfuse.create_trace_id() if lf_on else None
    tid = thread_id or os.getenv("THREAD_ID", f"deepagent-poc-{mode}")
    tags = [f"mode:{mode}", "scenario:release-readiness"]
    config = {
        "configurable": {
            "thread_id": tid,
            "trace_id": trace_id,
            "original_user_input": user,
            "user_id": "poc-user",
            "mode": mode,
            "tags": tags,
        }
    }

    client = None
    session_cm = None
    if lf_on and trace_id:
        client, session_cm = begin_session(
            trace_id=trace_id,
            user_input=user,
            mode=mode,
            user_id="poc-user",
            session_id=tid,
            tags=tags,
        )

    t0 = time.perf_counter()
    error = None
    result: dict = {}
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": user}]},
            config=config,
        )
    except Exception as exc:  # noqa: BLE001
        error = exc
    latency_ms = int((time.perf_counter() - t0) * 1000)

    output = ""
    if error is None:
        output = print_reply(result)
    else:
        print(f"\n[error] {error}\n")
        output = str(error)

    if lf_on and trace_id:
        end_session(
            client,
            session_cm,
            trace_id=trace_id,
            output=output,
            input_text=user,
        )
        host = os.getenv("LANGFUSE_HOST") or "http://localhost:3000"
        print(f"[langfuse] flushed trace {trace_id} mode={mode}")
        print(f"[langfuse] {host}/trace/{trace_id}\n")

    tokens = (
        extract_usage_tokens(result)
        if error is None
        else {"prompt_tokens": 0, "completion_tokens": 0}
    )
    return {
        "mode": mode,
        "latency_ms": latency_ms,
        "output": output,
        "trace_id": trace_id,
        "error": error,
        **tokens,
    }


def run_repl(agent, *, mode: str, banner: str) -> None:
    if (
        not os.getenv("GROQ_API_KEY")
        and not os.getenv("OPENAI_API_KEY")
        and not os.getenv("OPENAI_BASE_URL")
    ):
        raise SystemExit(
            "Set GROQ_API_KEY (preferred) or OPENAI_API_KEY in .env (see .env.example)."
        )

    lf_on = langfuse_enabled()
    print(f"Langfuse tracing: {'ON' if lf_on else 'OFF'}")
    if lf_on:
        host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
        print(f"  → view traces at {host} (filter tag mode:{mode})")
    print(banner)
    print(
        "Try: We're shipping payments-service v2.4 tomorrow. "
        "Check failed pipelines and pending approvals, search for payment-API "
        "outages or CVEs, then give a release-risk insight with a chart.\n"
    )
    print("Type a question, or 'exit' to quit.\n")

    while True:
        try:
            user = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not user:
            continue
        if user.lower() in {"exit", "quit", "q"}:
            print("Bye.")
            break
        stats = invoke_agent(agent, user, mode=mode)
        print(
            f"[stats] mode={stats['mode']} latency_ms={stats['latency_ms']} "
            f"prompt_tokens≈{stats['prompt_tokens']} "
            f"completion_tokens≈{stats['completion_tokens']}\n"
        )
