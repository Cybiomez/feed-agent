"""Суммаризатор через локальный Claude на боксе (claude CLI, headless `-p`).

Отдельная сессия Claude прямо на машине: ключ не нужен, идёт через подписку. Каждая
статья -> один вызов `claude -p` с текстом на stdin (без лимитов длины аргумента и без
проблем с кавычками) -> строгий JSON с русской выжимкой.
"""

from __future__ import annotations

import json
import subprocess

from ..models import Item
from .base import Summarizer

# Инструкция с литеральным JSON (без .format — иначе скобки JSON ломают подстановку).
_INSTRUCTION = (
    "Ты — редактор новостного дайджеста на русском языке. Прочитай статью и верни СТРОГО "
    "один JSON-объект, без пояснений вокруг и без markdown-ограждения:\n"
    '{"title":"<заголовок по-русски>",'
    '"summary":"<2-3 предложения сути по-русски, самодостаточно, без отсылки к статье>",'
    '"takeaways":["<короткий тезис>","<ещё>"],'
    '"conclusion":"<короткий вывод/аналитика: почему это важно>"}\n'
    "Пиши по-русски, кратко и по делу, без воды.\n\n"
)


def _extract_json_object(text: str) -> dict:
    """Достать первый JSON-объект из ответа модели, даже если вокруг есть лишнее/ограждение."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("в ответе нет JSON-объекта")
    return json.loads(text[start : end + 1])


class ClaudeSummarizer(Summarizer):
    """Русская выжимка через локальный `claude -p`."""

    name = "claude"

    def __init__(self, timeout_s: int = 120, model: str = "") -> None:
        self.timeout_s = timeout_s
        self.model = model  # пусто = модель по умолчанию claude CLI

    def summarize(self, item: Item, fulltext: str) -> dict | None:
        body = fulltext or item.summary or item.title
        prompt = _INSTRUCTION + f"ЗАГОЛОВОК ОРИГИНАЛА: {item.title}\n\nТЕКСТ СТАТЬИ:\n{body}\n"
        cmd = ["claude", "-p"]
        if self.model:
            cmd += ["--model", self.model]
        try:
            proc = subprocess.run(
                cmd, input=prompt, text=True,
                capture_output=True, timeout=self.timeout_s,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            obj = _extract_json_object(proc.stdout)
        except (ValueError, TypeError):
            return None
        # Нормализуем поля: строки/список, обрезаем длину.
        title = str(obj.get("title", "")).strip()[:200]
        summary = str(obj.get("summary", "")).strip()[:700]
        conclusion = str(obj.get("conclusion", "")).strip()[:500]
        takeaways = obj.get("takeaways", [])
        if not isinstance(takeaways, list):
            takeaways = []
        takeaways = [str(t).strip()[:200] for t in takeaways if str(t).strip()][:5]
        if not summary:
            return None
        return {"title": title or item.title, "summary": summary,
                "takeaways": takeaways, "conclusion": conclusion}
