"""Langfuse + OTEL helpers for sdlc-mcp (client and server)."""

from __future__ import annotations

import os

_client = None
_tried = False


def langfuse_enabled() -> bool:
    return os.getenv("LANGFUSE_ENABLED", "false").lower() in ("1", "true", "yes")


def tracing_mode() -> str:
    """``production`` = client + server SEP-414; ``client`` = caller-only wire."""
    return os.getenv("MCP_TRACING_MODE", "production").strip().lower() or "production"


def langfuse_server_enabled() -> bool:
    """Executor spans on the MCP server process.

    Default ON in ``MCP_TRACING_MODE=production``. Override with
    ``LANGFUSE_TRACE_SERVER=true|false``. Server never creates a root without
    parent ``_meta`` (SEP-414).
    """
    explicit = os.getenv("LANGFUSE_TRACE_SERVER", "").strip().lower()
    if explicit in ("1", "true", "yes"):
        return True
    if explicit in ("0", "false", "no"):
        return False
    return tracing_mode() == "production" and langfuse_enabled()


def get_langfuse():
    """Lazy Langfuse client (None if disabled / misconfigured)."""
    global _client, _tried
    if _tried:
        return _client
    _tried = True
    if not langfuse_enabled():
        return None
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        print("[langfuse] LANGFUSE_ENABLED but keys missing — tracing off")
        return None
    try:
        from langfuse import Langfuse
    except ImportError:
        print("[langfuse] package not installed — tracing off")
        return None

    host = (
        os.getenv("LANGFUSE_HOST")
        or os.getenv("LANGFUSE_BASE_URL")
        or "https://cloud.langfuse.com"
    )
    _client = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
    return _client


def flush() -> None:
    lf = get_langfuse()
    if lf is not None:
        lf.flush()


def child_server_env(base: dict | None = None) -> dict:
    """Env for the stdio MCP server subprocess (Langfuse keys + prod tracing)."""
    env = dict(base or os.environ)
    if langfuse_enabled():
        env["LANGFUSE_ENABLED"] = "true"
        if langfuse_server_enabled():
            env["LANGFUSE_TRACE_SERVER"] = "true"
            env["MCP_TRACING_MODE"] = "production"
    return env
