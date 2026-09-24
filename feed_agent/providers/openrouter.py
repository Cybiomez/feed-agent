"""Провайдер оценки через OpenRouter (OpenAI-совместимый API), модель QWEN по умолчанию.

Ключ берётся из окружения (имя переменной — в settings.model.api_key_env), в коде и
конфиге его нет. Оцениваем пачкой: одна пачка заголовков = один запрос (дёшево).
"""

from __future__ import annotations

import json
import os
import re

import requests

from ..models import Item
from .base import LLMProvider

# Инструкция модели. Профиль вкуса подмешивается ниже. Просим строгий JSON — его парсим.
_SYSTEM = (
    "Ты — персональный фильтр новостей. Оцени каждую новость по тому, насколько она "
    "релевантна вкусу пользователя, описанному в профиле ниже. Оценка — целое 0..100 "
    "(0 — явный мусор, 100 — точно интересно). ВАЖНО: при сомнении ставь оценку ВЫШЕ "
    "порога, лучше пропустить лишнее, чем потерять нужное. Ответь СТРОГО JSON-массивом "
    "объектов вида {\"i\": <номер>, \"score\": <0..100>, \"reason\": \"<кратко по-русски>\"}, "
    "без пояснений вокруг.\n\n=== ПРОФИЛЬ ВКУСА ===\n"
)


def _extract_json_array(text: str) -> list:
    """Достать JSON-массив из ответа модели, даже если вокруг есть лишний текст."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("в ответе модели нет JSON-массива")
    return json.loads(text[start : end + 1])


class OpenRouterProvider(LLMProvider):
    """Оценщик поверх OpenRouter."""

    name = "openrouter"

    def __init__(self, model: str, api_base: str, api_key_env: str, timeout_s: int) -> None:
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.timeout_s = timeout_s
        self.api_key = os.environ.get(api_key_env, "").strip()
        if not self.api_key:
            raise RuntimeError(
                f"Нет ключа провайдера: переменная окружения {api_key_env} пуста. "
                f"В бою её наполняет брокер secret (Vaultwarden) через EnvironmentFile."
            )

    def score_batch(self, items: list[Item], profile: str) -> dict[str, tuple[int, str]]:
        # Нумерованный список новостей для модели (номер -> uid держим у себя).
        lines = []
        for i, it in enumerate(items, 1):
            summary = (it.summary or "")[:280]
            lines.append(f"{i}. [{it.source_name}] {it.title} — {summary}")
        user = "Новости:\n" + "\n".join(lines)

        resp = requests.post(
            f"{self.api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                # Необязательные заголовки OpenRouter для идентификации приложения.
                "X-Title": "feed-agent",
            },
            json={
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _SYSTEM + profile},
                    {"role": "user", "content": user},
                ],
            },
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        arr = _extract_json_array(content)

        # Сопоставляем номера ответа с uid по порядку исходного списка.
        out: dict[str, tuple[int, str]] = {}
        for obj in arr:
            try:
                idx = int(obj["i"]) - 1
                if 0 <= idx < len(items):
                    score = max(0, min(100, int(obj.get("score", 0))))
                    reason = re.sub(r"\s+", " ", str(obj.get("reason", ""))).strip()[:200]
                    out[items[idx].uid] = (score, reason)
            except (KeyError, ValueError, TypeError):
                continue
        return out
