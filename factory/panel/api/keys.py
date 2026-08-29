"""Ключи доступа: что живо, что истекло и как обновить.

Самая частая поломка системы — истёкший ключ загрузки картинок в ВК: он живёт
сутки. Поэтому обновление сделано так же, как в боте, и той же функцией:
владелец приносит адрес из строки браузера, панель достаёт из него одноразовый
код и обменивает его на ключ **сама, со своего адреса**.

Это важная деталь, купленная отладкой: ВК привязывает ключ к тому, кто его
получил. Готовый ключ, взятый в браузере под VPN, к системе не подойдёт, а код
не привязан ни к чему.

Сами ключи наружу не отдаются — только последние символы. Панель открыта в
браузере, а браузеры хранят историю и кэш.
"""

from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from factory.core import http, secrets, vk_auth
from factory.core.config import ProjectConfig, resolve_secret
from factory.core.errors import FactoryError
from factory.core.logging import get_logger
from factory.panel import deps

log = get_logger(__name__)
router = APIRouter()


class Key(BaseModel):
    env: str
    title: str
    purpose: str
    present: bool
    #: Только хвост. Показывать ключ целиком незачем: узнать его владелец не
    #: может ниоткуда, а увидеть в чужих руках — легко.
    tail: str | None
    #: Проверен ли живым вызовом. ``None`` — проверка для этого ключа не делается.
    alive: bool | None = None
    note: str | None = None


class Keys(BaseModel):
    slug: str
    keys: list[Key]
    #: Ссылка, по которой владелец берёт одноразовый код для ключа загрузки.
    vk_code_url: str | None


class VkCode(BaseModel):
    #: Весь адрес из строки браузера или один код: просить человека выделить
    #: подстроку — верный способ получить код, обрезанный на символ.
    text: str = Field(min_length=1)


class Saved(BaseModel):
    ok: bool
    what_next: str


def _tail(value: str | None) -> str | None:
    return f"…{value[-4:]}" if value and len(value) >= 4 else None


def _config(slug: str) -> ProjectConfig:
    configs = deps.projects()
    if slug not in configs:
        raise HTTPException(status_code=404, detail=f"Проект «{slug}» не подключён.")
    return configs[slug]


def _upload_key_alive(config: ProjectConfig, token: str) -> tuple[bool, str]:
    """Живой ли ключ загрузки. Проверяется вызовом, а не сроком.

    Срок хранить негде — ВК его не сообщает, — да и не в сроке дело: ключ
    умирает и от смены адреса. Единственный честный ответ даёт сам ВК.
    """
    try:
        with http.client_for("vk", proxy_env=config.vk.proxy_env) as client:
            response = client.get(
                "https://api.vk.com/method/photos.getWallUploadServer",
                params={
                    "group_id": config.vk.group_id,
                    "access_token": token,
                    "v": config.vk.api_version,
                },
            )
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — проверка не должна ронять экран
        return False, f"Не удалось связаться с ВК: {exc}"

    if "error" in payload:
        return False, payload["error"].get("error_msg", "ВКонтакте отказал")
    return True, "Ключ принят, картинки загружаются."


@router.get("/api/groups/{slug}/keys", response_model=Keys)
def list_keys(slug: str, conn: sqlite3.Connection = Depends(deps.session)) -> Keys:
    config = _config(slug)

    upload = os.environ.get(config.vk.upload_token_env or "", "")
    alive, note = (None, "Ключ не задан.")
    if upload:
        alive, note = _upload_key_alive(config, upload)

    items = [
        Key(
            env=config.vk.upload_token_env or "",
            title="Ключ загрузки картинок в ВК",
            purpose="Живёт сутки. Без него посты встают на шаге публикации.",
            present=bool(upload),
            tail=_tail(upload),
            alive=alive,
            note=note,
        ),
        Key(
            env=config.vk.token_env,
            title="Ключ сообщества",
            purpose="Публикует посты. Бессрочный.",
            present=bool(os.environ.get(config.vk.token_env, "")),
            tail=_tail(os.environ.get(config.vk.token_env, "")),
        ),
        Key(
            env=config.llm.api_key_env or "",
            title="Ключ провайдера моделей",
            purpose="Тексты, проверка фактов и картинки. Один на всё.",
            present=bool(os.environ.get(config.llm.api_key_env or "", "")),
            tail=_tail(os.environ.get(config.llm.api_key_env or "", "")),
            note=(
                "Остаток месячного лимита провайдер сообщает только в тексте "
                "отказа, когда лимит уже исчерпан. Сколько потрачено — на "
                "экране расходов."
            ),
        ),
    ]

    if config.telegram is not None:
        items.append(
            Key(
                env=config.telegram.token_env,
                title="Токен Telegram-бота",
                purpose="Уведомления. Решения принимаются здесь, в панели.",
                present=bool(os.environ.get(config.telegram.token_env, "")),
                tail=_tail(os.environ.get(config.telegram.token_env, "")),
            )
        )

    return Keys(
        slug=slug,
        keys=items,
        vk_code_url=vk_auth.authorize_url(config.vk.app_id) if config.vk.app_id else None,
    )


@router.post("/api/groups/{slug}/vk-code", response_model=Saved)
def accept_vk_code(
    slug: str, body: VkCode, conn: sqlite3.Connection = Depends(deps.session)
) -> Saved:
    """Обменять одноразовый код на ключ загрузки и сохранить его."""
    config = _config(slug)

    if not config.vk.app_id or not config.vk.app_secret_env:
        raise HTTPException(
            status_code=409,
            detail=(
                "У проекта не заданы vk.app_id и vk.app_secret_env — обменять код "
                "не на что. Пропишите их в настройках группы."
            ),
        )

    code = vk_auth.extract_code(body.text)
    if code is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "В присланном тексте нет кода. Нужен весь адрес из строки "
                "браузера — тот, что начинается на https://oauth.vk.ru/blank.html"
            ),
        )

    try:
        token = vk_auth.exchange(
            code,
            app_id=config.vk.app_id,
            secret=resolve_secret(config.vk.app_secret_env, context="приложения ВК"),
            proxy_env=config.vk.proxy_env,
        )
        secrets.update_secret(config.vk.upload_token_env, token)
    except FactoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    log.info("ключ загрузки обновлён из панели", extra={"slug": slug})
    return Saved(
        ok=True,
        what_next=(
            "Ключ обновлён. Посты, ждавшие публикации, пойдут дальше сами — "
            "перезапускать ничего не нужно."
        ),
    )
