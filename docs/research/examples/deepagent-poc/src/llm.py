"""Chat model factory — prefer native Groq (like codepath generate.py) for tool calling."""

from __future__ import annotations

import os


def build_chat_model(*, temperature: float = 0.2, streaming: bool = False):
    """Return a LangChain chat model.

    If ``GROQ_API_KEY`` is set, use ``langchain_groq.ChatGroq`` (native Groq API).
    That avoids broken tool-call JSON that shows up when routing Groq through the
    OpenAI-compatible base URL.

    Otherwise fall back to ``init_chat_model(MODEL)`` (OpenAI / LiteLLM / etc.).
    """
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if groq_key:
        from langchain_groq import ChatGroq

        # Strip optional "openai:" / "groq:" prefixes from MODEL
        model = os.getenv("MODEL", "llama-3.3-70b-versatile").strip()
        for prefix in ("openai:", "groq:", "langchain_groq:"):
            if model.startswith(prefix):
                model = model[len(prefix) :]
                break
        return ChatGroq(
            api_key=groq_key,
            model=model,
            temperature=temperature,
            streaming=streaming,
        )

    from langchain.chat_models import init_chat_model

    model_id = os.getenv("MODEL", "openai:gpt-4o-mini")
    return init_chat_model(model_id, temperature=temperature)
