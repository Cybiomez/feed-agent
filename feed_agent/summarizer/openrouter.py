"""Суммаризатор через OpenRouter (OpenAI-совместимый API), бесплатная модель QWEN.

Для обкатки без расхода подписки Claude. Ключ — из окружения (имя в settings), в коде нет.
Вся логика/промпты — общие (_common.PromptSummarizer); здесь только HTTP-вызов модели.
"""

from __future__ import annotations

import os
import time

import requests

from ._common import PromptSummarizer


class OpenRouterSummarizer(PromptSummarizer):
    name = "openrouter"

    def __init__(self, model: str, api_base: str, api_key_env: str, timeout_s: int = 60) -> None:
        self.model = model or "qwen/qwen-2.5-72b-instruct:free"
        self.api_base = (api_base or "https://openrouter.ai/api/v1").rstrip("/")
        self.timeout_s = timeout_s
        self.api_key = os.environ.get(api_key_env, "").strip()
        if not self.api_key:
            raise RuntimeError(
                f"Нет ключа модели: переменная окружения {api_key_env} пуста. "
                f"Бесплатный ключ OpenRouter — в Vaultwarden, через secret в EnvironmentFile."
            )

    def _complete(self, prompt: str) -> str | None:
        # Бесплатный тир любит отвечать 429 (лимит частоты) — ждём и повторяем, иначе пачки
        # выжимок молча теряются. Уважаем Retry-After, если сервер его даёт.
        for attempt in range(4):
            try:
                resp = requests.post(
                    f"{self.api_base}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json", "X-Title": "feed-agent"},
                    json={"model": self.model, "temperature": 0,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=self.timeout_s,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = int(resp.headers.get("Retry-After", 0) or 0) or (2 ** attempt) * 3
                    time.sleep(min(wait, 30))
                    continue
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            except Exception:
                if attempt < 3:
                    time.sleep(3)
                    continue
                return None
        return None
