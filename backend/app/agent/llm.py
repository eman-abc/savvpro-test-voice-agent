"""Chat-model factory. One OpenAI-compatible client covers OpenAI, Groq and Ollama (they all speak that API), so there is a
single code path; only the base URL and key differ. Keys come from the environment and are never logged."""

from __future__ import annotations

from typing import Any

from app.config import Settings

PRESETS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "ollama": "http://localhost:11434/v1",
}


def make_chat_model(settings: Settings) -> Any | None:
    if settings.llm_provider in ("none", ""):
        return None
    if settings.llm_provider not in PRESETS:
        raise ValueError(f"LLM_PROVIDER must be one of none, {', '.join(PRESETS)}")
    if not settings.llm_model:
        raise ValueError("LLM_MODEL is required when an LLM provider is selected")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key or "not-needed-for-local",  # Ollama ignores the key but the client requires one
        base_url=settings.llm_base_url or PRESETS[settings.llm_provider],
        temperature=0,  # scheduling should be repeatable, not creative
        max_retries=1,
        timeout=20,
    )
