"""Суммаризатор через локальный Claude на боксе (claude CLI, headless `-p`).

Отдельная сессия Claude прямо на машине: ключ не нужен, идёт через подписку. Делает две
вещи:
  1) filter_relevant — по профилю вкуса отсеивает нерелевантное (пачкой, по заголовкам);
  2) summarize — по релевантной статье пишет русскую выжимку в нужном формате
     (заголовок = суть + пара строк значимости; тейки/вывод — для «Подробнее»).
Текст уходит на stdin (без лимитов длины аргумента и без проблем с кавычками).
"""

from __future__ import annotations

import json
import subprocess

from ..models import Item
from .base import Summarizer

# --- выжимка одной статьи ---
_SUMMARY_INSTRUCTION = (
    "Ты — редактор новостного дайджеста на русском языке. По тексту статьи верни СТРОГО "
    "один JSON-объект, без пояснений вокруг и без markdown-ограждения:\n"
    '{"headline":"<заголовок-суть по-русски: сама суть новости одной ёмкой строкой, '
    'не кликбейт>",'
    '"explain":"<1-2 предложения: что именно произошло и в чём интерес — прорыв, '
    'достижение, сдвиг, на что обращено внимание. Самодостаточно, по-русски>",'
    '"takeaways":["<короткий тезис>","<ещё>"],'
    '"conclusion":"<короткий вывод/аналитика: почему это важно>"}\n'
    "Пиши по-русски, кратко и по делу, без воды и без отсылок к статье.\n\n"
)

# --- пакетный фильтр релевантности по заголовкам ---
# Решение — ТОЛЬКО по профилю пользователя (никаких захардкоженных категорий: что «за» и
# что «против» — целиком в профиле, иначе легко отсечь нужное).
_FILTER_INSTRUCTION = (
    "Ниже — профиль интересов пользователя и список новостей (по заголовкам). Для каждой "
    "реши, релевантна ли она ИНТЕРЕСАМ ИЗ ПРОФИЛЯ (ориентируйся строго на профиль, а не на "
    "общие представления). Верни СТРОГО JSON-массив номеров релевантных новостей, например "
    "[1,3,4], без пояснений. При сомнении — ВКЛЮЧАЙ (лучше лишнее, чем потерять нужное).\n\n"
)


def _run_claude(prompt: str, timeout_s: int, model: str) -> str | None:
    cmd = ["claude", "-p"]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, input=prompt, text=True,
                             capture_output=True, timeout=timeout_s)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout


def _extract_json(text: str, opener: str, closer: str):
    start = text.find(opener)
    end = text.rfind(closer)
    if start == -1 or end == -1 or end < start:
        raise ValueError("нет JSON в ответе")
    return json.loads(text[start : end + 1])


class ClaudeSummarizer(Summarizer):
    """Фильтр релевантности и русская выжимка через локальный `claude -p`."""

    name = "claude"

    def __init__(self, timeout_s: int = 120, model: str = "") -> None:
        self.timeout_s = timeout_s
        self.model = model  # пусто = модель по умолчанию claude CLI

    def filter_relevant(self, items: list[Item], profile: str) -> list[Item]:
        """Оставить только релевантные профилю новости (пачкой, по заголовкам)."""
        if not items:
            return []
        lines = []
        for i, it in enumerate(items, 1):
            snippet = (it.summary or "")[:160]
            lines.append(f"{i}. [{it.source_name}] {it.title} — {snippet}")
        prompt = (_FILTER_INSTRUCTION + "=== ПРОФИЛЬ ИНТЕРЕСОВ ===\n" + profile +
                  "\n\n=== НОВОСТИ ===\n" + "\n".join(lines) + "\n")
        out = _run_claude(prompt, self.timeout_s, self.model)
        if not out:
            return items  # фильтр недоступен — не теряем: пропускаем всё дальше
        try:
            nums = _extract_json(out, "[", "]")
            keep = {int(n) for n in nums}
        except (ValueError, TypeError):
            return items
        return [it for i, it in enumerate(items, 1) if i in keep]

    def summarize(self, item: Item, fulltext: str) -> dict | None:
        body = fulltext or item.summary or item.title
        prompt = _SUMMARY_INSTRUCTION + f"ЗАГОЛОВОК ОРИГИНАЛА: {item.title}\n\nТЕКСТ СТАТЬИ:\n{body}\n"
        out = _run_claude(prompt, self.timeout_s, self.model)
        if not out:
            return None
        try:
            obj = _extract_json(out, "{", "}")
        except (ValueError, TypeError):
            return None
        headline = str(obj.get("headline", "")).strip()[:200]
        explain = str(obj.get("explain", "")).strip()[:600]
        conclusion = str(obj.get("conclusion", "")).strip()[:500]
        takeaways = obj.get("takeaways", [])
        if not isinstance(takeaways, list):
            takeaways = []
        takeaways = [str(t).strip()[:200] for t in takeaways if str(t).strip()][:5]
        if not explain and not headline:
            return None
        # headline -> title (суть-заголовок), explain -> summary (для дайджеста)
        return {"title": headline or item.title, "summary": explain,
                "takeaways": takeaways, "conclusion": conclusion}
