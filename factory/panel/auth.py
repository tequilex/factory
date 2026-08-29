"""Вход в панель: пароль и подписанная кука.

Панель управляет публикациями в живые сообщества, поэтому вход обязателен даже
за Tailscale. Tailscale отсекает чужих на уровне сети, пароль закрывает то, чего
сеть не видит: потерянный или отданный на минуту разблокированный телефон.

Отсюда два намеренных упрощения. Блокировки после N неудач нет — перебирать
пароль может только тот, кто уже внутри вашей сети. Таблицы сессий тоже нет: всё,
что нужно знать про вход, лежит в самой куке, а её подлинность проверяется
подписью. Меньше состояния — меньше того, что может разъехаться.

Пароль хранится хешем в файле секретов. Имя переменной содержит ``PASSWORD``, и
это не косметика: предохранитель в ``core/logging.py`` затирает такие значения,
если они когда-нибудь попадут в лог.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets as stdlib_secrets
import time

from factory.core import paths, secrets
from factory.core.errors import ConfigError

#: Имя куки. Версия в значении, а не в имени: сменится формат — старые куки
#: перестанут подходить сами, без возни с удалением чужих имён.
COOKIE_NAME = "factory_panel"

PASSWORD_ENV = "PANEL_PASSWORD_HASH"
SECRET_ENV = "PANEL_SECRET"

#: Параметры scrypt. Малина считает медленно, а вход бывает раз в месяц — но
#: и подбирать пароль будет то же слабое железо. 16 МБ памяти на проверку это
#: заметно дороже для перебора, чем для одного честного входа.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32

DEFAULT_DAYS = 30

#: Насколько живёт кука без отметки «доверять этому устройству». Ровно сутки:
#: столько же живёт ключ загрузки ВК, и владельцу проще помнить одно число.
SHORT_HOURS = 24


def hash_password(password: str) -> str:
    """Хеш пароля вместе с солью и параметрами.

    Параметры хранятся в самой строке, а не в коде: если однажды придётся их
    поднять, старые пароли продолжат проверяться, а не отвалятся все разом.
    """
    if not password:
        raise ConfigError(
            "Пустой пароль не годится.",
            why="Панель управляет публикациями в живые сообщества.",
            what_to_do="Придумай пароль и повтори: factory panel-password",
        )

    salt = stdlib_secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Совпадает ли пароль с сохранённым хешем.

    Любая непонятная строка — не совпадение, а не исключение: испорченный файл
    секретов не должен превращаться в трейсбек на экране входа.
    """
    try:
        algorithm, n, r, p, salt_hex, digest_hex = stored.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(digest_hex) // 2,
        )
    except (ValueError, TypeError, MemoryError):
        return False

    # Сравнение постоянного времени: обычное «==» выходит на первом несовпавшем
    # байте и по времени ответа выдаёт, сколько символов угадано.
    return hmac.compare_digest(digest.hex(), digest_hex)


def set_password(password: str) -> None:
    """Записать новый пароль и, если его ещё не было, завести секрет подписи."""
    secrets.update_secret(PASSWORD_ENV, hash_password(password))
    if not os.environ.get(SECRET_ENV):
        secrets.update_secret(SECRET_ENV, stdlib_secrets.token_urlsafe(32))


def reset_secret() -> None:
    """Сменить секрет подписи — это и есть «выйти на всех устройствах».

    Отдельной таблицы сессий нет, поэтому отозвать все куки можно только так:
    меняется ключ, которым они подписаны, и ни одна старая больше не сходится.
    """
    secrets.update_secret(SECRET_ENV, stdlib_secrets.token_urlsafe(32))


def _stored_hash() -> str:
    stored = os.environ.get(PASSWORD_ENV, "")
    if not stored:
        raise ConfigError(
            "Пароль от панели не задан.",
            why=f"В файле {paths.env_file()} нет строки {PASSWORD_ENV}.",
            what_to_do="Задай пароль: factory panel-password",
        )
    return stored


def _signing_key() -> bytes:
    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        raise ConfigError(
            "Не задан секрет подписи для панели.",
            why=f"В файле {paths.env_file()} нет строки {SECRET_ENV}.",
            what_to_do="Задай пароль заново, секрет заведётся сам: factory panel-password",
        )
    return secret.encode("utf-8")


def check_password(password: str) -> bool:
    return verify_password(password, _stored_hash())


def issue_cookie(*, trusted: bool = False, now: float | None = None) -> str:
    """Значение куки: до какого времени она годна плюс подпись.

    В куке нет ничего, кроме срока. Ни имени, ни прав, ни идентификатора сессии:
    пользователь у панели один, и всё, что она решает, — впускать или нет.
    """
    moment = time.time() if now is None else now
    lifetime = DEFAULT_DAYS * 86400 if trusted else SHORT_HOURS * 3600
    expires = int(moment + lifetime)
    payload = f"v1.{expires}"
    signature = hmac.new(_signing_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def check_cookie(value: str | None, *, now: float | None = None) -> bool:
    """Годна ли кука. Ложь на любую порчу, а не исключение.

    «Не исключение» здесь не общие слова: сюда приходит что угодно из браузера,
    включая обрезанные и чужие значения. Любое из них должно означать «пройди
    вход», а не пятисотую ошибку с трейсбеком.

    Срок сравнивается последним, уже после подписи: иначе по ответу на истёкшую
    подделку можно было бы отличить верную подпись от неверной.
    """
    if not value:
        return False

    parts = value.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return False

    # Разбор срока идёт до подписи, и это не нарушает порядок проверок: здесь
    # проверяется не срок, а форма. Без этого произвольная строка доезжала до
    # hmac и роняла проверку исключением — кириллица в куке давала
    # UnicodeEncodeError вместо честного «не годится».
    try:
        expires = int(parts[1])
    except ValueError:
        return False

    # Подпись считается по канонической записи: «01» и «1» дают один и тот же
    # срок, но подписан был ровно один из них.
    payload = f"v1.{expires}"
    expected = hmac.new(_signing_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts[2]):
        return False

    return (time.time() if now is None else now) < expires
