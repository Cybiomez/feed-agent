"""Хранилище на SQLite: новости, оценки, реакции, служебные пары ключ-значение.

Одна база — единая точка правды о том, что уже видели (дедупликация) и что уже
отправляли. Формат открытый и переносимый (§25). Схема создаётся при первом запуске.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Enriched, Item, Score

# База лежит рядом с репозиторием, в data/ (в git не попадает).
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "feed.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    uid          TEXT PRIMARY KEY,      -- хеш url+заголовок (дедупликация)
    source_name  TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    summary      TEXT DEFAULT '',
    published    TEXT DEFAULT '',
    collected_at TEXT NOT NULL,
    delivered    INTEGER DEFAULT 0      -- 1 = уже ушло в дайджест
);
CREATE TABLE IF NOT EXISTS scores (
    item_uid  TEXT PRIMARY KEY REFERENCES items(uid),
    score     INTEGER NOT NULL,
    reason    TEXT DEFAULT '',
    model     TEXT DEFAULT '',
    scored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrichment (  -- русская выжимка по полному тексту (Claude)
    item_uid     TEXT PRIMARY KEY REFERENCES items(uid),
    fulltext     TEXT DEFAULT '',        -- извлечённый полный текст статьи
    image_url    TEXT DEFAULT '',        -- главная картинка (для «Подробнее»)
    ru_title     TEXT DEFAULT '',        -- заголовок по-русски
    ru_summary   TEXT DEFAULT '',        -- 2–3 предложения сути (для дайджеста)
    ru_takeaways TEXT DEFAULT '',        -- JSON-список тейков (для «Подробнее»)
    ru_conclusion TEXT DEFAULT '',       -- вывод/аналитика (для «Подробнее»)
    enriched_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reactions (   -- этап 2: 👍/👎 на пункты дайджеста
    item_uid TEXT PRIMARY KEY REFERENCES items(uid),
    reaction TEXT NOT NULL,              -- 'up' | 'down'
    at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (          -- служебное состояние (последний прогон и т.п.)
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def _now() -> str:
    # микросекунды — чтобы порядок по времени внутри одного прогона был осмысленным
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Storage:
    """Тонкая обёртка над SQLite с операциями, нужными пайплайну."""

    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # --- новости ---

    def exists(self, uid: str) -> bool:
        cur = self._db.execute("SELECT 1 FROM items WHERE uid = ?", (uid,))
        return cur.fetchone() is not None

    def add_item(self, item: Item) -> bool:
        """Добавить новость, если её ещё нет. True — добавили, False — уже была (дубль)."""
        if self.exists(item.uid):
            return False
        self._db.execute(
            "INSERT INTO items (uid, source_name, title, url, summary, published, collected_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (item.uid, item.source_name, item.title, item.url,
             item.summary, item.published, item.collected_at or _now()),
        )
        self._db.commit()
        return True

    def unscored_items(self, limit: int) -> list[Item]:
        """Новости без оценки и ещё не отправленные — кандидаты на прогон через модель."""
        cur = self._db.execute(
            "SELECT i.* FROM items i LEFT JOIN scores s ON s.item_uid = i.uid"
            " WHERE s.item_uid IS NULL AND i.delivered = 0"
            " ORDER BY i.collected_at DESC LIMIT ?",
            (limit,),
        )
        return [self._row_to_item(r) for r in cur.fetchall()]

    def save_score(self, score: Score) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO scores (item_uid, score, reason, model, scored_at)"
            " VALUES (?,?,?,?,?)",
            (score.item_uid, int(score.score), score.reason, score.model, score.scored_at or _now()),
        )
        self._db.commit()

    def selected_for_digest(self, threshold: int, limit: int) -> list[tuple[Item, Score]]:
        """Прошедшие порог и ещё не отправленные — то, что пойдёт в дайджест."""
        cur = self._db.execute(
            "SELECT i.*, s.score AS s_score, s.reason AS s_reason, s.model AS s_model,"
            "       s.scored_at AS s_at"
            " FROM items i JOIN scores s ON s.item_uid = i.uid"
            " WHERE i.delivered = 0 AND s.score >= ?"
            " ORDER BY s.score DESC, i.collected_at DESC LIMIT ?",
            (threshold, limit),
        )
        out: list[tuple[Item, Score]] = []
        for r in cur.fetchall():
            item = self._row_to_item(r)
            score = Score(item_uid=r["uid"], score=r["s_score"], reason=r["s_reason"],
                          model=r["s_model"], scored_at=r["s_at"])
            out.append((item, score))
        return out

    # --- обогащение (русская выжимка) ---

    def to_enrich(self, limit: int) -> list[Item]:
        """Свежие ещё не обогащённые и не отправленные новости — кандидаты на выжимку.

        Честно по источникам: берём по несколько самых свежих с КАЖДОГО источника, чтобы
        многочисленные ленты (RSS) не вытесняли из отбора малочисленные каналы. Порядок —
        по «рангу свежести внутри источника» (сначала по одной новейшей с каждого), потом
        по времени — так топ отбора распределён по источникам."""
        row = self._db.execute(
            "SELECT COUNT(DISTINCT source_name) FROM items i"
            " LEFT JOIN enrichment e ON e.item_uid = i.uid"
            " WHERE i.delivered = 0 AND e.item_uid IS NULL"
        ).fetchone()
        nsrc = (row[0] if row else 0) or 1
        per_source = max(3, limit // nsrc)   # сколько новейших брать с каждого источника
        cur = self._db.execute(
            "SELECT * FROM ("
            "  SELECT i.*, ROW_NUMBER() OVER ("
            "    PARTITION BY i.source_name ORDER BY i.collected_at DESC, i.rowid DESC) AS rn"
            "  FROM items i LEFT JOIN enrichment e ON e.item_uid = i.uid"
            "  WHERE i.delivered = 0 AND e.item_uid IS NULL"
            ") WHERE rn <= ? ORDER BY rn, collected_at DESC LIMIT ?",
            (per_source, limit),
        )
        return [self._row_to_item(r) for r in cur.fetchall()]

    def save_enrichment(self, uid: str, fulltext: str, image_url: str, ru: dict) -> None:
        """Сохранить результат выжимки. ru = {title, summary, takeaways[list], conclusion}."""
        self._db.execute(
            "INSERT OR REPLACE INTO enrichment"
            " (item_uid, fulltext, image_url, ru_title, ru_summary, ru_takeaways,"
            "  ru_conclusion, enriched_at) VALUES (?,?,?,?,?,?,?,?)",
            (uid, fulltext or "", image_url or "", ru.get("title", ""),
             ru.get("summary", ""), json.dumps(ru.get("takeaways", []), ensure_ascii=False),
             ru.get("conclusion", ""), _now()),
        )
        self._db.commit()

    def enriched_for_digest(self, limit: int) -> list[Enriched]:
        """Обогащённые, но ещё не отправленные новости — то, что пойдёт в дайджест."""
        cur = self._db.execute(
            "SELECT i.uid, i.source_name, i.url, e.image_url, e.ru_title, e.ru_summary,"
            "       e.ru_takeaways, e.ru_conclusion"
            " FROM items i JOIN enrichment e ON e.item_uid = i.uid"
            " WHERE i.delivered = 0 AND e.ru_summary != ''"
            " ORDER BY i.collected_at DESC LIMIT ?",
            (limit,),
        )
        out: list[Enriched] = []
        for r in cur.fetchall():
            try:
                takeaways = json.loads(r["ru_takeaways"] or "[]")
            except (ValueError, TypeError):
                takeaways = []
            out.append(Enriched(
                uid=r["uid"], source_name=r["source_name"], url=r["url"],
                ru_title=r["ru_title"], ru_summary=r["ru_summary"],
                ru_takeaways=takeaways, ru_conclusion=r["ru_conclusion"],
                image_url=r["image_url"],
            ))
        return out

    def get_enriched(self, uid: str) -> Enriched | None:
        """Одна обогащённая новость по id — для разворота «Подробнее»."""
        cur = self._db.execute(
            "SELECT i.uid, i.source_name, i.url, e.image_url, e.ru_title, e.ru_summary,"
            "       e.ru_takeaways, e.ru_conclusion"
            " FROM items i JOIN enrichment e ON e.item_uid = i.uid"
            " WHERE i.uid = ? AND e.ru_summary != ''",
            (uid,),
        )
        r = cur.fetchone()
        if not r:
            return None
        try:
            takeaways = json.loads(r["ru_takeaways"] or "[]")
        except (ValueError, TypeError):
            takeaways = []
        return Enriched(
            uid=r["uid"], source_name=r["source_name"], url=r["url"],
            ru_title=r["ru_title"], ru_summary=r["ru_summary"],
            ru_takeaways=takeaways, ru_conclusion=r["ru_conclusion"],
            image_url=r["image_url"],
        )

    def add_reaction(self, uid: str, reaction: str) -> None:
        """Сохранить реакцию 👍/👎 на новость (для будущего обучения вкуса)."""
        self._db.execute(
            "INSERT OR REPLACE INTO reactions (item_uid, reaction, at) VALUES (?,?,?)",
            (uid, reaction, _now()),
        )
        self._db.commit()

    def mark_delivered(self, uids: list[str]) -> None:
        self._db.executemany("UPDATE items SET delivered = 1 WHERE uid = ?",
                             [(u,) for u in uids])
        self._db.commit()

    def purge_old(self, history_days: int) -> int:
        """Удалить новости старше N дней (и их оценки/реакции) — чтобы база не пухла."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=history_days)).isoformat()
        cur = self._db.execute("SELECT uid FROM items WHERE collected_at < ?", (cutoff,))
        old = [r["uid"] for r in cur.fetchall()]
        if old:
            q = ",".join("?" * len(old))
            self._db.execute(f"DELETE FROM scores WHERE item_uid IN ({q})", old)
            self._db.execute(f"DELETE FROM reactions WHERE item_uid IN ({q})", old)
            self._db.execute(f"DELETE FROM items WHERE uid IN ({q})", old)
            self._db.commit()
        return len(old)

    # --- служебное ---

    def kv_set(self, k: str, v: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, v))
        self._db.commit()

    def kv_get(self, k: str, default: str = "") -> str:
        cur = self._db.execute("SELECT v FROM kv WHERE k = ?", (k,))
        row = cur.fetchone()
        return row["v"] if row else default

    @staticmethod
    def _row_to_item(r: sqlite3.Row) -> Item:
        return Item(
            source_name=r["source_name"], title=r["title"], url=r["url"],
            summary=r["summary"], published=r["published"], collected_at=r["collected_at"],
        )
