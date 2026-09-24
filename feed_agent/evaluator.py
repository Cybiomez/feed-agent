"""Оценщик: гоняет новости через провайдера пачками и сохраняет оценки.

Здесь же — смещение в полноту: если провайдер упал или не вернул оценку по новости,
ставим нейтральную оценку на уровне порога (новость проходит), а не теряем её.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import Settings
from .models import Item, Score
from .providers.base import LLMProvider
from .storage import Storage


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def evaluate(items: list[Item], profile: str, provider: LLMProvider,
             settings: Settings, storage: Storage) -> int:
    """Оценить новости и сохранить оценки. Возвращает число оценённых."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scored = 0
    for batch in _chunks(items, settings.batch_size):
        try:
            result = provider.score_batch(batch, profile)
        except Exception as e:
            # Провайдер недоступен — не теряем новости: пропускаем их с нейтральной
            # оценкой на уровне порога (при сомнении — оставлять).
            result = {}
            fallback_reason = f"оценщик недоступен ({type(e).__name__}) — пропущено по умолчанию"
        else:
            fallback_reason = "оценщик не вернул оценку — пропущено по умолчанию"

        for it in batch:
            if it.uid in result:
                score, reason = result[it.uid]
            else:
                score, reason = settings.threshold, fallback_reason
            storage.save_score(Score(
                item_uid=it.uid, score=score, reason=reason,
                model=provider.name, scored_at=now,
            ))
            scored += 1
    return scored
