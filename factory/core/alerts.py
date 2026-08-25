"""Тревоги владельцу: то, из-за чего система встала и сама не выберется.

Главное свойство — **не повторяться**. Тик идёт раз в минуту; тревога без
защиты от повтора превращается в поток одинаковых сообщений, который перестают
читать через час. А тревога, которую перестали читать, хуже её отсутствия:
создаёт ощущение, что за системой следят.

Поэтому у каждой тревоги есть ключ. Пока причина не исчезла, сообщение уходит
один раз; когда исчезла — отметка снимается, и следующий случай снова прозвучит.

Чего здесь намеренно нет: тревоги на «N постов ждут ревью». Это нормальная
рабочая ситуация, а не авария — именно так и появляется шум.
"""

from __future__ import annotations

import sqlite3

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.errors import FactoryError
from factory.core.logging import get_logger

log = get_logger(__name__)

_PREFIX = "alert:"

# Ссылка, по которой владелец получает новый ключ загрузки. client_id — это
# приложение проекта; scope нужен ровно тот, что требуют загрузка и стена.
VK_TOKEN_URL = (
    "https://oauth.vk.ru/authorize?client_id=54066965&scope=photos,wall,offline"
    "&redirect_uri=https://oauth.vk.ru/blank.html&display=page&response_type=token&v=5.199"
)


def _key(name: str, scope: str) -> str:
    return f"{_PREFIX}{name}:{scope}"


def is_raised(conn: sqlite3.Connection, name: str, scope: str) -> bool:
    row = conn.execute("SELECT 1 FROM meta WHERE key = ?", (_key(name, scope),)).fetchone()
    return row is not None


def raise_once(
    conn: sqlite3.Connection, notifier, *, chat_id: int, name: str, scope: str, text: str
) -> bool:
    """Отправить тревогу, если она ещё не висит. ``True`` — отправили.

    Отметка ставится **после** успешной отправки. Наоборот было бы хуже: сбой
    сети погасил бы тревогу, о которой владелец так и не узнал.
    """
    if is_raised(conn, name, scope):
        return False

    try:
        notifier.alert(chat_id=chat_id, text=text)
    except FactoryError as exc:
        log.warning(
            "не удалось отправить тревогу",
            extra={"alert": name, "scope": scope, "reason": str(exc)},
        )
        return False

    stamp = to_iso(now_utc())
    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (_key(name, scope), stamp, stamp),
        )
    log.info("тревога отправлена", extra={"alert": name, "scope": scope})
    return True


def clear(conn: sqlite3.Connection, name: str, scope: str) -> None:
    """Причина исчезла: следующий такой случай снова прозвучит."""
    with db.write_transaction(conn):
        conn.execute("DELETE FROM meta WHERE key = ?", (_key(name, scope),))


def vk_token_expired_text(project: str, token_env: str) -> str:
    """Текст про протухший ключ ВК: что делать, а не что сломалось."""
    return (
        f"⚠️ [{project}] Публикация встала: ключ загрузки картинок в ВК истёк.\n\n"
        "Он живёт 24 часа, продлить нельзя — так устроен ВК.\n\n"
        "Что сделать (полминуты):\n"
        "1. Открыть ссылку ниже и разрешить доступ.\n"
        "2. Скопировать из адресной строки браузера ВЕСЬ адрес.\n"
        "3. Прислать его мне сюда одним сообщением.\n\n"
        f"{VK_TOKEN_URL}\n\n"
        f"Ключ подставится сам, посты поедут дальше. "
        f"Переменная: {token_env}"
    )


#: Сколько пост может простоять на одном месте, прежде чем это станет странным.
#: Сутки выбраны намеренно щедро: ожидание человека — не авария, и торопить
#: владельца сообщениями через час было бы навязчиво.
STUCK_AFTER_HOURS = 24


def nothing_to_publish_text(project: str, free_topics: int, in_flight: int) -> str:
    return (
        f"⚠️ [{project}] Скоро публиковать будет нечего.\n\n"
        f"Свободных тем: {free_topics}. Постов в работе: {in_flight}.\n\n"
        "Что сделать: добавить темы.\n"
        f"  factory topics import {project} темы.txt\n\n"
        "Файл — по теме в строке."
    )


def stuck_post_text(project: str, post_id: int, state: str, hours: int, title: str | None) -> str:
    what = {
        "in_review": "ждёт вашего решения — кнопки в сообщении выше",
        "approved": "одобрен, но не публикуется",
    }.get(state, f"застрял на шаге «{state}»")
    return (
        f"⏳ [{project}] Пост {post_id} {what} уже {hours} ч.\n\n"
        f"«{title or 'без заголовка'}»\n\n"
        "Это не ошибка, система его не бросила. Но если так и задумано — можно "
        "не отвечать, я больше не напомню."
    )


def failed_post_text(project: str, post_id: int, title: str | None, error: str | None) -> str:
    return (
        f"❌ [{project}] Пост {post_id} сломался окончательно.\n\n"
        f"«{title or 'без заголовка'}»\n\n"
        f"{(error or 'причина не записана').strip()[:900]}\n\n"
        f"Починив причину, вернуть пост в работу: factory post retry {post_id}"
    )
