# feed-agent — личный фильтр новостей по вкусу

Модуль экосистемы MyCelium (пайплайн №6, слой L4 — суммаризация; доставка через шов
`notify`, слой L1). Собирает поток новостей из источников, оценивает каждую новость
моделью против «профиля вкуса», отсекает мусор, сжимает прошедшее в выжимки и шлёт
**дайджест в Telegram**. Со временем дообучается на реакциях 👍/👎.

Полное ТЗ: `~/projects/notes/tz-news-filter.md`.

## Как устроено (крупно)

```
источники (RSS → потом TG) ─▶ сборщик ─▶ SQLite (дедуп по хешу)
                                            │
                     профиль вкуса ─▶ оценщик (LLM через сменный провайдер)
                                            │  порог отбора (смещён в полноту)
                          реакции 👍/👎 ◀─ дайджест ─▶ notify ─▶ Telegram
                     запуск: systemd timer 2–3×/день + ручной прогон
```

- **Источники** — через сменные адаптеры (`feed_agent/collectors/`): RSS сейчас, Telegram
  позже. Механизм не зависит от способа чтения — добавить тип = добавить адаптер.
- **Провайдер модели** — сменный (`feed_agent/providers/`): по умолчанию **QWEN через
  OpenRouter** (OpenAI-совместимый API); есть офлайн-заглушка для сухих прогонов/тестов.
- **Профиль вкуса** — редактируемый файл (`config/profile.md`) + накопленные реакции.
  Главный принцип: **при сомнении — оставлять** (не терять нужное).
- **Доставка** — только через существующий `notify` (шов не дублируем).

## Установка (MVP)

```bash
cd ~/projects/feed-agent
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp config/settings.example.toml config/settings.toml
cp config/sources.example.toml  config/sources.toml
cp config/profile.example.md    config/profile.md
```

Ключ провайдера — **не в репозитории**. Берётся из окружения `OPENROUTER_API_KEY`,
которое в бою наполняет брокер `secret` (Vaultwarden) через systemd. Локально:

```bash
export OPENROUTER_API_KEY=...   # только в сессии, не коммитить
```

## Запуск

```bash
python -m feed_agent --once            # один полный прогон: собрать → оценить → дайджест → notify
python -m feed_agent --once --dry-run  # то же, но без модели (заглушка) и без отправки — печать в консоль
python -m feed_agent --collect-only    # только собрать в базу, без оценки/доставки
```

По расписанию — через `systemd/feed-agent.timer` (см. файлы в `systemd/`).

## Версии и ветки

semver из git-тега, git-flow (`feat/*`/`fix/*` от `dev` → PR в `dev`; в `main` — только
пользователь). Журнал решений и осознанных упрощений — `docs/decisions.md`.
