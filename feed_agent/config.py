"""Загрузка конфигурации: настройки, источники, профиль вкуса.

Все три — в открытых форматах и вне кода (§25). Реальные файлы (settings.toml,
sources.toml, profile.md) в git не попадают; если их нет — берём *.example, чтобы
сухой прогон работал сразу после клона.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

# Корень репозитория (…/feed-agent), от него — папка config.
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def _pick(name: str, example: str) -> Path:
    """Вернуть реальный файл конфига, а если его нет — пример (для прогона из коробки)."""
    real = CONFIG_DIR / name
    return real if real.exists() else CONFIG_DIR / example


@dataclass
class Source:
    """Один источник новостей из sources.toml."""

    type: str
    name: str
    url: str


@dataclass
class Settings:
    """Разобранные настройки из settings.toml (с дефолтами на случай пропусков)."""

    max_items_per_run: int = 60
    history_days: int = 14
    threshold: int = 45
    digest_max_items: int = 12
    provider: str = "openrouter"
    model: str = "qwen/qwen-2.5-72b-instruct:free"
    batch_size: int = 20
    timeout_s: int = 40
    api_key_env: str = "OPENROUTER_API_KEY"
    api_base: str = "https://openrouter.ai/api/v1"
    notify_cmd: str = "notify"
    title: str = "🗞️ Дайджест новостей"
    target_chat: str = ""      # разовый адрес получателя (id чата), пусто = дефолт notify
    target_thread: str = ""    # тема форума у получателя, если нужна


def load_settings() -> Settings:
    """Прочитать settings.toml (или пример) и сложить в Settings, не падая на пропусках."""
    path = _pick("settings.toml", "settings.example.toml")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    run = raw.get("run", {})
    flt = raw.get("filter", {})
    mdl = raw.get("model", {})
    dlv = raw.get("delivery", {})
    d = Settings()  # дефолты
    return Settings(
        max_items_per_run=run.get("max_items_per_run", d.max_items_per_run),
        history_days=run.get("history_days", d.history_days),
        threshold=flt.get("threshold", d.threshold),
        digest_max_items=flt.get("digest_max_items", d.digest_max_items),
        provider=mdl.get("provider", d.provider),
        model=mdl.get("model", d.model),
        batch_size=mdl.get("batch_size", d.batch_size),
        timeout_s=mdl.get("timeout_s", d.timeout_s),
        api_key_env=mdl.get("api_key_env", d.api_key_env),
        api_base=mdl.get("api_base", d.api_base),
        notify_cmd=dlv.get("notify_cmd", d.notify_cmd),
        title=dlv.get("title", d.title),
        target_chat=str(dlv.get("target_chat", d.target_chat)),
        target_thread=str(dlv.get("target_thread", d.target_thread)),
    )


def load_sources() -> list[Source]:
    """Прочитать список источников из sources.toml (или примера)."""
    path = _pick("sources.toml", "sources.example.toml")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    out: list[Source] = []
    for s in raw.get("source", []):
        out.append(Source(type=s["type"], name=s["name"], url=s["url"]))
    return out


def load_profile() -> str:
    """Прочитать профиль вкуса (markdown) целиком — он идёт в промпт модели."""
    path = _pick("profile.md", "profile.example.md")
    return path.read_text(encoding="utf-8")
