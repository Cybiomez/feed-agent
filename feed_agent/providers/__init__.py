"""Фабрика провайдеров оценки. Выбор — по settings.model.provider.

Добавить провайдера (DashScope, локальный, Claude) = написать класс и дописать ветку.
"""

from __future__ import annotations

from ..config import Settings
from .base import LLMProvider
from .stub import StubProvider


def make_provider(settings: Settings, force_stub: bool = False) -> LLMProvider:
    """Собрать провайдера по настройкам. force_stub=True — принудительно офлайн-заглушка
    (сухой прогон): не требует ключа/сети."""
    if force_stub or settings.provider == "stub":
        return StubProvider()
    if settings.provider == "openrouter":
        # Импорт внутри — чтобы отсутствие requests не мешало сухому прогону на заглушке.
        from .openrouter import OpenRouterProvider

        return OpenRouterProvider(
            model=settings.model,
            api_base=settings.api_base,
            api_key_env=settings.api_key_env,
            timeout_s=settings.timeout_s,
        )
    raise ValueError(f"Неизвестный провайдер модели: {settings.provider!r}")
