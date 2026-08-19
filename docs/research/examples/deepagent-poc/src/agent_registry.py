"""Agent registry — name/description roster for the supervisor (no tool schemas)."""

from __future__ import annotations

from typing import Any

_deep_agent_configs: list[dict[str, Any]] = []


def register_agent(
    *,
    name: str,
    description: str,
    tools: list | None = None,
    system_prompt: str | None = None,
    model=None,
    middleware: list | None = None,
    capabilities: list[str] | None = None,
    example_tasks: list[str] | None = None,
) -> None:
    """Register a declarative subagent for `create_deep_agent(subagents=...)`."""
    cfg: dict[str, Any] = {
        "name": name,
        "description": description,
        "tools": tools or [],
        "system_prompt": system_prompt or "",
        "middleware": middleware or [],
    }
    if model is not None:
        cfg["model"] = model
    if capabilities:
        cfg["capabilities"] = capabilities
    if example_tasks:
        cfg["example_tasks"] = example_tasks
    # Idempotent: replace existing entry with the same name
    for i, existing in enumerate(_deep_agent_configs):
        if existing.get("name") == name:
            _deep_agent_configs[i] = cfg
            return
    _deep_agent_configs.append(cfg)


def get_all_agent_configs() -> list[dict[str, Any]]:
    return list(_deep_agent_configs)


def build_delegation_block() -> str:
    """Roster text for the supervisor system prompt (name + description only)."""
    lines = ["Available specialists (route via the task tool):"]
    for cfg in _deep_agent_configs:
        lines.append(f"- {cfg['name']}: {cfg['description']}")
        caps = cfg.get("capabilities") or []
        if caps:
            lines.append(f"  Capabilities: {', '.join(caps)}")
    return "\n".join(lines)


def reset_registry() -> None:
    """Test helper — clear registrations."""
    _deep_agent_configs.clear()
