from __future__ import annotations

from engine.config.schemas import LlmConfig
from engine.config.settings import AppMode, Settings
from engine.llm.base import LLMProvider
from engine.llm.mock import MockProvider
from engine.llm.service import LLMService
from engine.llm.usage import UsageLedger


def create_llm_service(settings: Settings, cfg: LlmConfig, provider: LLMProvider | None = None) -> LLMService:
    if provider is None:
        if settings.llm_api_key is None or settings.app_mode is AppMode.REPLAY and settings.llm_provider == "mock":
            provider = MockProvider()
        elif settings.llm_provider == "anthropic":
            from engine.llm.anthropic_provider import AnthropicProvider

            provider = AnthropicProvider(api_key=settings.llm_api_key.get_secret_value())
        else:
            raise ValueError(f"unsupported LLM_PROVIDER {settings.llm_provider}")
    ledger = UsageLedger(cfg.pricing, settings.llm_monthly_budget_usd)
    return LLMService(provider, cfg, ledger, settings.llm_model_fast, settings.llm_model_smart)
