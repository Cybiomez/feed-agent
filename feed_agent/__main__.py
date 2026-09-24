"""CLI фид-агента.

    python -m feed_agent --once            полный прогон (собрать → оценить → дайджест → notify)
    python -m feed_agent --once --dry-run  то же на офлайн-заглушке, без отправки (печать в консоль)
    python -m feed_agent --collect-only    только собрать новости в базу
"""

from __future__ import annotations

import argparse
import sys

from .pipeline import run_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="feed_agent", description="Фильтр новостей по вкусу")
    parser.add_argument("--once", action="store_true", help="один полный прогон")
    parser.add_argument("--dry-run", action="store_true",
                        help="без модели (заглушка) и без отправки — печать дайджеста")
    parser.add_argument("--collect-only", action="store_true",
                        help="только собрать новости в базу, без оценки и доставки")
    args = parser.parse_args(argv)

    if not (args.once or args.collect_only):
        parser.print_help()
        return 2

    summary = run_once(dry_run=args.dry_run, collect_only=args.collect_only)
    # Короткая сводка в консоль/журнал systemd.
    print("feed-agent:", ", ".join(f"{k}={v}" for k, v in summary.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
