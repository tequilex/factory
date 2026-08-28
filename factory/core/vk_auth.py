"""Обмен одноразового кода ВК на ключ загрузки.

Существует ради одной особенности, которая ломала всю задумку с обновлением
ключа из телефона: **ВК привязывает ключ к адресу, с которого его получили.**

Раньше владелец получал готовый ключ прямо в браузере. Дома, без VPN, адрес
совпадал с адресом машины — и всё работало. Стоило открыть ссылку из отпуска,
с мобильного интернета или под VPN — ключ выписывался на тот адрес, а система
ходит в ВК со своего. ВК отвечал «access_token was given to another ip address»,
и выглядело это как «ключ сразу протух».

Теперь владелец получает не ключ, а **одноразовый код**. Код ни к чему не
привязан. Присылает его боту, и ключ выписывает себе сама система — тем самым
на свой адрес, с которого потом и ходит.

Проверено живьём: код получен в браузере под VPN в другой стране, обменян на
Raspberry Pi, ВК принял ключ с домашнего адреса малины.

Срок жизни это не меняет — сутки, как и раньше, ``refresh_token`` ВК не даёт.
"""

from __future__ import annotations

import re

from factory.core import http
from factory.core.errors import ProviderError
from factory.core.logging import get_logger

log = get_logger(__name__)

OAUTH_BASE = "https://oauth.vk.com"

#: Куда ВК возвращает код. Пустая страница самого ВК: своего сайта у системы
#: нет, а адрес должен совпадать в обоих запросах — при выдаче кода и при
#: обмене. Иначе ВК отвечает «redirect_uri mismatch».
REDIRECT_URI = "https://oauth.vk.com/blank.html"


def authorize_url(app_id: int) -> str:
    """Ссылка, по которой владелец получает код.

    ``response_type=code``, а не ``token``: код можно получить откуда угодно,
    ключ по нему выпишет система.
    """
    return (
        f"{OAUTH_BASE}/authorize?client_id={app_id}&display=page"
        f"&redirect_uri=https%3A%2F%2Foauth.vk.com%2Fblank.html"
        "&scope=photos&response_type=code&v=5.199"
    )


def extract_code(text: str) -> str | None:
    """Вынуть код из того, что прислал владелец.

    Принимается и весь адрес из браузера, и один код: адрес приходит с
    телефона, и просить человека выделить подстроку — верный способ получить
    код, обрезанный на символ.
    """
    match = re.search(r"[?&#]code=([A-Za-z0-9._-]+)", text)
    candidate = match.group(1) if match else text.strip()

    # Код ВК — шестнадцатеричная строка. Проверка по виду отсекает случайно
    # присланную ссылку и обрезанный при копировании код.
    return candidate if re.fullmatch(r"[0-9a-f]{16,}", candidate) else None


def exchange(code: str, *, app_id: int, secret: str, proxy_env: str | None = None) -> str:
    """Обменять код на ключ загрузки. Возвращает сам ключ.

    Ходит напрямую, без прокси для Telegram: ключ обязан привязаться к тому
    адресу, с которого система потом обращается к ВК. Пусти этот запрос через
    заграничный прокси — и получится ровно та беда, от которой уходим.
    """
    with http.client_for("vk", proxy_env=proxy_env) as client:
        response = client.post(
            f"{OAUTH_BASE}/access_token",
            data={
                "client_id": app_id,
                "client_secret": secret,
                "redirect_uri": REDIRECT_URI,
                "code": code,
            },
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(
            "ВКонтакте ответил не по делу на обмен кода.",
            why=f"Код {response.status_code}, начало ответа: {response.text[:120]!r}",
            what_to_do="Обычно это временный сбой. Попробуйте получить код заново.",
        ) from exc

    if "access_token" in payload:
        log.info(
            "ключ загрузки получен обменом кода",
            extra={"expires_in": payload.get("expires_in")},
        )
        return str(payload["access_token"])

    raise ProviderError(
        "ВКонтакте отказался обменять код на ключ.",
        why=f"{payload.get('error')}: {payload.get('error_description', '')}",
        what_to_do=_advice(str(payload.get("error", ""))),
    )


def _advice(error: str) -> str:
    if error == "invalid_grant":
        return (
            "Код уже использован или устарел — он одноразовый и живёт недолго. "
            "Откройте ссылку заново и пришлите свежий код."
        )
    if error == "invalid_client":
        return (
            "Не совпал защищённый ключ приложения. Проверьте строку с ним в "
            "файле секретов: он берётся в кабинете dev.vk.com у вашего "
            "приложения."
        )
    if error == "invalid_request":
        return (
            "Запрос не принят. Чаще всего это несовпадение адреса возврата: "
            "он должен быть одинаковым при получении кода и при обмене."
        )
    return "Попробуйте получить код заново. Если повторяется — напишите, разберёмся."
