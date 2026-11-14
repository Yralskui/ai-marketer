"""Выбор LLM-провайдера по настройкам."""

from __future__ import annotations

from app.config import Settings
from app.llm.base import LLMProvider


def create_llm(settings: Settings) -> LLMProvider:
    """Новый провайдер: создать app/llm/<name>_provider.py и добавить ветку здесь."""
    provider = settings.llm_provider
    if provider == "mock":
        from app.llm.mock_provider import MockLLMProvider

        return MockLLMProvider()
    if provider == "openai":
        from app.llm.openai_provider import OpenAIProvider

        if (settings.llm_model_light or settings.llm_model).startswith(
            ("gpt-6.1-sol", "gpt-6-astra")
        ):
            from app.llm.responses_provider import ResponsesProvider

            return ResponsesProvider(settings)
        return OpenAIProvider(
            api_key=settings.llm_api_key,
            model=settings.llm_model_light or settings.llm_model,
            base_url=settings.llm_base_url or None,
            timeout=settings.llm_timeout,
            max_retries=settings.llm_max_retries,
            temperature=settings.llm_temperature,
            proxies=settings.llm_proxies,
            retry_budget=settings.llm_retry_budget_seconds,
        )
    raise ValueError(f"Неизвестный LLM_PROVIDER: {provider!r}")
