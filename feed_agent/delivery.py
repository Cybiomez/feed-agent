"""Доставка дайджеста через существующий шов notify (слой L1).

Свою отправку не пишем — только вызываем `notify`, который владеет адресом и токеном.
В сухом прогоне (dry_run) ничего не шлём, печатаем в консоль.
"""

from __future__ import annotations

import subprocess


def deliver(text: str, notify_cmd: str, to_chat: str = "", to_thread: str = "",
            dry_run: bool = False) -> bool:
    """Отправить текст через notify. True — успех. dry_run — печать вместо отправки.

    to_chat/to_thread — разовый адрес получателя (notify --to). Пусто — дефолтный адрес
    notify (тот, куда идут алерты)."""
    if dry_run:
        print("\n----- DRY-RUN: дайджест (не отправлен) -----\n")
        print(text)
        print("\n----- конец дайджеста -----\n")
        return True
    cmd = [notify_cmd]
    if to_chat:                               # разовый получатель — тема «Новости»
        cmd += ["--to", str(to_chat)]
        if to_thread:
            cmd.append(str(to_thread))
    try:
        # Текст уходит в notify через stdin (notify умеет и pipe).
        proc = subprocess.run(
            cmd, input=text, text=True,
            capture_output=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"delivery: не удалось вызвать {notify_cmd}: {e}")
        return False
    if proc.returncode != 0:
        print(f"delivery: notify вернул код {proc.returncode}: {proc.stderr.strip()}")
        return False
    return True
