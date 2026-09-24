"""Скачивание и извлечение полного текста статьи (+ главной картинки).

Бокс качает статью сам (у него доступ не режется, как из РФ) — значит суть можно
перенести в сообщение, а зарубежная ссылка пользователю не нужна. Извлечение —
через trafilatura (чистит от меню/рекламы, вытаскивает основной текст и метаданные).
"""

from __future__ import annotations

import trafilatura

# Ограничение длины текста, который отдаём модели (экономия контекста/времени).
_MAX_TEXT = 6000


def fetch_article(url: str) -> tuple[str, str]:
    """Вернуть (полный_текст, url_картинки). Пустой текст — если скачать/разобрать не вышло
    (тогда обогащение пропускаем, ничего не роняя)."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return "", ""
        text = trafilatura.extract(downloaded, include_comments=False,
                                   include_images=False, favor_precision=True) or ""
        image = ""
        try:
            meta = trafilatura.extract_metadata(downloaded)
            if meta and meta.image:
                image = meta.image
        except Exception:
            pass
        return text[:_MAX_TEXT], image
    except Exception:
        return "", ""
