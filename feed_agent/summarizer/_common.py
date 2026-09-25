"""Общая логика суммаризаторов: промпты, разбор ответа, фильтр/группировка/выжимка.

Конкретный провайдер (Claude CLI, OpenRouter/QWEN, …) реализует только `_complete(prompt)` —
«выполнить промпт, вернуть текст ответа». Вся остальная механика и промпты — здесь, чтобы
модели давали одинаковый результат и не расходились.
"""

from __future__ import annotations

import json
from abc import abstractmethod

from ..models import Item
from .base import Summarizer

# --- выжимка одной статьи ---
SUMMARY_INSTRUCTION = (
    "Ты — редактор новостного дайджеста на русском языке. По тексту статьи верни СТРОГО "
    "один JSON-объект, без пояснений вокруг и без markdown-ограждения:\n"
    '{"headline":"<заголовок-суть по-русски: сама суть новости одной ёмкой строкой, '
    'не кликбейт>",'
    '"key":"<ОДНА короткая строка с ключевыми цифрами/фактами: что, сколько, когда>",'
    '"explain":"<1-2 предложения: что произошло и в чём интерес — прорыв, достижение, '
    'сдвиг, на что обращено внимание. Самодостаточно, по-русски>",'
    '"takeaways":["<короткий тезис>","<ещё>"],'
    '"conclusion":"<короткий вывод/аналитика: почему это важно>"}\n'
    "Пиши по-русски, кратко и по делу, без воды и без отсылок к статье.\n\n"
)

# --- пакетная выжимка нескольких статей за один вызов (экономия: агент грузится 1 раз) ---
SUMMARY_BATCH_INSTRUCTION = (
    "Ты — редактор новостного дайджеста на русском языке. Ниже НЕСКОЛЬКО новостей "
    "(пронумерованы). По КАЖДОЙ сделай выжимку. Ответ — СТРОГО JSON-массив объектов, по "
    "одному на новость, без пояснений и без markdown-ограждения:\n"
    '[{"i":<номер>,"headline":"<заголовок-суть по-русски, не кликбейт>",'
    '"key":"<ОДНА строка: ключевые цифры/факты>",'
    '"explain":"<1-2 предложения: что произошло и в чём интерес/прорыв>",'
    '"takeaways":["<короткий тезис>"],"conclusion":"<короткий вывод: почему важно>"}, ...]\n'
    "Пиши по-русски, кратко, без воды. Охвати ВСЕ номера.\n\n"
)

# --- группировка дублей (одно событие в разных каналах) ---
GROUP_INSTRUCTION = (
    "Ниже список новостей (по заголовкам). Сгруппируй те, что освещают ОДНО И ТО ЖЕ "
    "конкретное событие/новость (а не просто общую тему). Верни СТРОГО JSON-массив групп "
    "номеров, каждый номер ровно в одной группе, например [[1,3],[2],[4,5]]. Если "
    "сомневаешься — считай РАЗНЫМИ (отдельные группы). Охвати ВСЕ номера.\n\n"
)

# --- пакетный фильтр релевантности по заголовкам (решение ТОЛЬКО по профилю) ---
FILTER_INSTRUCTION = (
    "Ниже — профиль интересов пользователя и список новостей (по заголовкам). Для каждой "
    "реши, релевантна ли она ИНТЕРЕСАМ ИЗ ПРОФИЛЯ (ориентируйся строго на профиль, а не на "
    "общие представления). Верни СТРОГО JSON-массив номеров релевантных новостей, например "
    "[1,3,4], без пояснений. При сомнении — ВКЛЮЧАЙ (лучше лишнее, чем потерять нужное).\n\n"
)


def extract_json(text: str, opener: str, closer: str):
    """Достать JSON (объект `{}` или массив `[]`) из ответа, даже если вокруг есть лишнее."""
    start = text.find(opener)
    end = text.rfind(closer)
    if start == -1 or end == -1 or end < start:
        raise ValueError("нет JSON в ответе")
    return json.loads(text[start : end + 1])


def _normalize(obj: dict, item: Item) -> dict | None:
    """Привести один разобранный объект выжимки к нашему формату (или None, если пусто)."""
    headline = str(obj.get("headline", "")).strip()[:200]
    key = str(obj.get("key", "")).strip()[:300]
    explain = str(obj.get("explain", "")).strip()[:600]
    conclusion = str(obj.get("conclusion", "")).strip()[:500]
    takeaways = obj.get("takeaways", [])
    if not isinstance(takeaways, list):
        takeaways = []
    takeaways = [str(t).strip()[:200] for t in takeaways if str(t).strip()][:5]
    if not explain and not headline:
        return None
    return {"title": headline or item.title, "summary": explain, "key": key,
            "takeaways": takeaways, "conclusion": conclusion}


class PromptSummarizer(Summarizer):
    """Суммаризатор поверх абстрактного `_complete`. Промпты и разбор — общие."""

    @abstractmethod
    def _complete(self, prompt: str) -> str | None:
        """Выполнить промпт и вернуть текстовый ответ модели (или None при сбое)."""
        raise NotImplementedError

    def filter_relevant(self, items: list[Item], profile: str) -> list[Item]:
        if not items:
            return []
        lines = [f"{i}. [{it.source_name}] {it.title} — {(it.summary or '')[:160]}"
                 for i, it in enumerate(items, 1)]
        prompt = (FILTER_INSTRUCTION + "=== ПРОФИЛЬ ИНТЕРЕСОВ ===\n" + profile +
                  "\n\n=== НОВОСТИ ===\n" + "\n".join(lines) + "\n")
        out = self._complete(prompt)
        if not out:
            return items  # фильтр недоступен — не теряем: пропускаем всё дальше
        try:
            keep = {int(n) for n in extract_json(out, "[", "]")}
        except (ValueError, TypeError):
            return items
        return [it for i, it in enumerate(items, 1) if i in keep]

    def group_duplicates(self, items: list[Item]) -> list[list[int]]:
        n = len(items)
        singletons = [[i] for i in range(n)]
        if n <= 1:
            return singletons
        lines = [f"{i}. [{it.source_name}] {it.title} — {(it.summary or '')[:120]}"
                 for i, it in enumerate(items, 1)]
        out = self._complete(GROUP_INSTRUCTION + "\n".join(lines) + "\n")
        if not out:
            return singletons
        try:
            groups = extract_json(out, "[", "]")
        except (ValueError, TypeError):
            return singletons
        result: list[list[int]] = []
        seen: set[int] = set()
        for g in groups:
            if not isinstance(g, list):
                return singletons
            idxs = []
            for num in g:
                try:
                    k = int(num) - 1
                except (ValueError, TypeError):
                    return singletons
                if k < 0 or k >= n or k in seen:
                    return singletons
                seen.add(k)
                idxs.append(k)
            if idxs:
                result.append(idxs)
        if seen != set(range(n)):
            return singletons  # покрыты не все — безопаснее без слияния
        return result

    def summarize(self, item: Item, fulltext: str) -> dict | None:
        body = fulltext or item.summary or item.title
        prompt = SUMMARY_INSTRUCTION + f"ЗАГОЛОВОК ОРИГИНАЛА: {item.title}\n\nТЕКСТ СТАТЬИ:\n{body}\n"
        out = self._complete(prompt)
        if not out:
            return None
        try:
            obj = extract_json(out, "{", "}")
        except (ValueError, TypeError):
            return None
        return _normalize(obj, item)

    def summarize_batch(self, articles: list[tuple[Item, str]]) -> list[dict | None]:
        """Сделать выжимки по НЕСКОЛЬКИМ статьям за ОДИН вызов модели (главная экономия:
        агент/системный промпт грузятся один раз, а не на каждую статью). Возвращает список
        той же длины: dict|None на каждую статью (None — если модель её не вернула/пусто)."""
        if not articles:
            return []
        parts = []
        for i, (it, body) in enumerate(articles, 1):
            text = (body or it.summary or it.title)[:3000]
            parts.append(f"=== НОВОСТЬ {i} ===\nЗАГОЛОВОК: {it.title}\nТЕКСТ:\n{text}")
        out = self._complete(SUMMARY_BATCH_INSTRUCTION + "\n\n".join(parts) + "\n")
        result: list[dict | None] = [None] * len(articles)
        if not out:
            return result
        try:
            arr = extract_json(out, "[", "]")
        except (ValueError, TypeError):
            return result
        for obj in arr:
            if not isinstance(obj, dict):
                continue
            try:
                idx = int(obj.get("i")) - 1
            except (ValueError, TypeError):
                continue
            if 0 <= idx < len(articles):
                result[idx] = _normalize(obj, articles[idx][0])
        return result
