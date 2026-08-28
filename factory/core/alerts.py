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
from factory.core.logging import get_logger

log = get_logger(__name__)

_PREFIX = "alert:"

# Ссылка авторизации ВК. Собирается по образцу из docs/ВК-как-это-работает.md,
# и каждая часть там проверена живьём:
#
# * домен **oauth.vk.com**, не .ru — на .ru тот же запрос отвечает Security Error;
# * ``redirect_uri`` закодирован, незакодированный ВК не принимает;
# * ``scope`` только ``photos``: право ``offline`` ВК отменил, и запрос
#   несуществующего права ломает всю ссылку.
#
# ``client_id`` подставляется из конфига проекта: приложение заводит владелец,
# и в общем коде его быть не может.
VK_AUTHORIZE = (
    "https://oauth.vk.com/authorize?client_id={app_id}&display=page"
    "&redirect_uri=https%3A%2F%2Foauth.vk.com%2Fblank.html"
    "&scope=photos&response_type=token&v=5.199"
)


def vk_token_url(app_id: int | None, *, by_code: bool = False) -> str | None:
    """Ссылка на получение ключа, если известно приложение владельца.

    ``by_code`` — просить одноразовый код вместо готового ключа. Тогда ключ
    выпишет себе сама система, и он привяжется к её адресу: обновлять можно
    откуда угодно, хоть из отпуска под чужим VPN.
    """
    if not app_id:
        return None
    if by_code:
        from factory.core.vk_auth import authorize_url

        return authorize_url(app_id)
    return VK_AUTHORIZE.format(app_id=app_id)


def _key(name: str, scope: str) -> str:
    return f"{_PREFIX}{name}:{scope}"


def is_raised(conn: sqlite3.Connection, name: str, scope: str) -> bool:
    row = conn.execute("SELECT 1 FROM meta WHERE key = ?", (_key(name, scope),)).fetchone()
    return row is not None


def raise_once(
    conn: sqlite3.Connection,
    notifier,
    *,
    chat_id: int,
    name: str,
    scope: str,
    text: str,
    fix_post_id: int | None = None,
) -> bool:
    """Отправить тревогу, если она ещё не висит. ``True`` — отправили.

    Отметка ставится **после** успешной отправки. Наоборот было бы хуже: сбой
    сети погасил бы тревогу, о которой владелец так и не узнал.
    """
    if is_raised(conn, name, scope):
        return False

    try:
        notifier.alert(chat_id=chat_id, text=text, fix_post_id=fix_post_id)
    except Exception as exc:  # noqa: BLE001 — см. ниже
        # Ловится всё, а не только FactoryError: провайдер отдаёт наружу и
        # httpx.ReadTimeout, а сеть до Telegram отвечает неровно. Уведомление о
        # поломке не имеет права стать поломкой само — иначе один обрыв связи
        # обрывает тик, хартбит не пишется, и на Этапе 7 healthcheck начнёт
        # перезапускать контейнер по кругу.
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


def vk_token_expired_text(
    project: str,
    token_env: str,
    app_id: int | None = None,
    *,
    by_code: bool = False,
    wrong_address: bool = False,
) -> str:
    """Текст про негодный ключ ВК: что делать, а не что сломалось.

    ``wrong_address`` — ключ не истёк, а выписан на другой адрес. Со стороны ВК
    это тот же код ошибки, но причина и лечение разные, и «истёк» про свежий
    ключ владельца только запутает.
    """
    if wrong_address:
        head = (
            f"⚠️ [{project}] Публикация встала: ключ ВК выписан на другой адрес.\n\n"
            "ВК привязывает ключ к тому, кто его получил. Похоже, ссылку "
            "открыли под VPN или с другого устройства.\n\n"
        )
    else:
        head = (
            f"⚠️ [{project}] Публикация встала: ключ загрузки картинок в ВК истёк.\n\n"
            "Он живёт 24 часа, продлить нельзя — так устроен ВК.\n\n"
        )
    link = vk_token_url(app_id, by_code=by_code)
    if link is None:
        # Без приложения ссылку не собрать. Врать готовым решением нельзя:
        # неработающая ссылка хуже честной отсылки к инструкции.
        return head + (
            "Что сделать: получить новый ключ по инструкции из RUNBOOK.md → "
            "«Обновить ключ загрузки ВК» и прислать его мне сюда.\n\n"
            f"Чтобы я присылал готовую ссылку, укажи vk.app_id в конфиге проекта.\n"
            f"Переменная: {token_env}"
        )
    tail = (
        "Откуда угодно: ключ выпишу себе я, и он привяжется к моему адресу."
        if by_code
        else "Важно: открывать без VPN, иначе ключ привяжется к чужому адресу."
    )
    return head + (
        "Что сделать (полминуты):\n"
        "1. Открыть ссылку ниже и разрешить доступ.\n"
        "2. Скопировать из адресной строки браузера ВЕСЬ адрес.\n"
        "3. Прислать его мне сюда одним сообщением.\n\n"
        f"{link}\n\n"
        f"{tail}\n"
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
        "Что сделать: пришлите мне новые темы одним сообщением, по теме в "
        "строке. Я переспрошу и добавлю.\n\n"
        "Посмотреть, что осталось: /topics"
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
        "Починив причину, вернуть пост в работу можно кнопкой ниже."
    )


def worker_silent_text(minutes: int) -> str:
    """Воркер не отвечает. Самая обидная поломка: бот бодр, а работы нет.

    Владелец нажимает кнопку, получает подтверждение и ничего не дожидается —
    потому что решение принимает бот, а исполняет воркер, и снаружи они
    неразличимы.
    """
    return (
        f"🔌 Воркер молчит уже {minutes} мин.\n\n"
        "Кнопки работают, но выполнять решения некому: посты не готовятся и не "
        "публикуются. Одобренное подождёт и уедет, когда он вернётся.\n\n"
        "Что делать: перезапустить воркер. Если он падает сразу, причина в "
        "логе — RUNBOOK.md → «Посты застряли и не двигаются»."
    )


def worker_back_text() -> str:
    return "🔌 Воркер вернулся, работа продолжается."
