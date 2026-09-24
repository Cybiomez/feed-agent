"""Суммаризатор через локальный Claude на боксе (claude CLI, headless `-p`).

Ключ не нужен — идёт через подписку. Вся логика (фильтр/группировка/выжимка и промпты) —
в _common.PromptSummarizer; здесь только «выполнить промпт через claude -p».
Текст уходит на stdin (без лимитов длины аргумента и без проблем с кавычками).
"""

from __future__ import annotations

import subprocess

from ._common import PromptSummarizer


class ClaudeSummarizer(PromptSummarizer):
    name = "claude"

    def __init__(self, timeout_s: int = 120, model: str = "") -> None:
        self.timeout_s = timeout_s
        self.model = model  # пусто = модель по умолчанию claude CLI

    def _complete(self, prompt: str) -> str | None:
        cmd = ["claude", "-p"]
        if self.model:
            cmd += ["--model", self.model]
        try:
            proc = subprocess.run(cmd, input=prompt, text=True,
                                 capture_output=True, timeout=self.timeout_s)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return proc.stdout
