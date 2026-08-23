"""The real VK publisher.

**Two tokens, not one.** VK splits the permissions across token types and neither
one can do the whole job:

* the community token can call ``wall.post`` but no upload method at all
  (error 27 on every single one);
* the user token can upload but is refused ``wall.post`` as a "non-standalone
  application".

Everything learned the hard way about this API — which methods are dead ends,
which errors mean what — is written down in ``docs/ВК-как-это-работает.md``.
Read it before changing anything here.

**Retries are asymmetric on purpose.** Getting an upload server, uploading bytes
and saving the photo are all idempotent: repeating them costs nothing but a
duplicate file in a system album. ``wall.post`` is not: a response that never
arrived does not mean the post was not created, and a retry would put a second
copy in the group. So the upload half retries generously and the publish half
does not retry at all.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx

from factory.core import http
from factory.core.errors import ProviderError
from factory.core.logging import get_logger

API_BASE = "https://api.vk.com/method"

# Загрузка идемпотентна — повторять можно щедро. Сеть до ВК из России отвечает
# нестабильно: рукопожатие TLS занимало до 11 секунд, часть запросов обрывалась.
UPLOAD_ATTEMPTS = 6
UPLOAD_PAUSE_SEC = 3.0

# Сервер загрузки умеет вернуть формально успешный ответ с пустым photo или
# вообще не-JSON. Оба раза помогает новый upload_url. Наблюдалось живьём.
EMPTY_PHOTO_ROUNDS = 4

log = get_logger(__name__)


class VkError(ProviderError):
    """Ошибка ВК с разобранным кодом."""

    def __init__(self, code: int, message: str, *, method: str) -> None:
        self.code = code
        super().__init__(
            f"ВКонтакте отказал в вызове {method}: ошибка {code}.",
            why=message,
            what_to_do=_advice(code, method),
        )


def _advice(code: int, method: str) -> str:
    """Каждая частая ошибка ВК — с инструкцией, а не с кодом наедине."""
    if code == 5:
        return (
            "Ключ доступа истёк или отозван. Ключ загрузки живёт 24 часа — "
            "получи новый и положи в файл секретов. "
            "См. RUNBOOK.md → «Обновить ключ загрузки ВК»."
        )
    if code == 27:
        return (
            "Метод недоступен ключу сообщества. Загрузка картинок выполняется "
            "ключом пользователя (vk.upload_token_env), публикация — ключом "
            "сообщества (vk.token_env). Похоже, ключи перепутаны местами. "
            "См. docs/ВК-как-это-работает.md"
        )
    if code == 15:
        return (
            "Доступ запрещён. Проверь, что группа указана верно и ключ выдан "
            "именно для неё."
        )
    if code == 214:
        return (
            "ВКонтакте не разрешил запись на стену. Обычно это дневной лимит "
            "публикаций сообщества либо ограничение на само сообщество."
        )
    if code in (6, 9):
        return (
            "Слишком много запросов подряд. Система повторит позже сама; если "
            "повторяется постоянно — увеличь FACTORY_TICK_INTERVAL_SEC."
        )
    return f"См. описание ошибки {code} в документации ВК для метода {method}."


class VkPublisher:
    """Публикация в сообщество ВКонтакте."""

    name = "vk"

    def __init__(
        self,
        *,
        group_id: int,
        token: str,
        upload_token: str,
        api_version: str = "5.199",
        proxy_env: str | None = None,
        sleep=time.sleep,
    ) -> None:
        self.group_id = group_id
        self.token = token
        self.upload_token = upload_token
        self.api_version = api_version
        self.proxy_env = proxy_env
        self._sleep = sleep

    def _client(self) -> httpx.Client:
        return http.client_for("vk", proxy_env=self.proxy_env)

    def _call(self, client: httpx.Client, method: str, token: str, **params) -> Any:
        """Один вызов API. Ошибку ВК превращает в понятное исключение."""
        response = client.post(
            f"{API_BASE}/{method}",
            data={**params, "access_token": token, "v": self.api_version},
        )
        response.raise_for_status()

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"ВКонтакте вернул не-JSON в ответ на {method}.",
                why=f"Первые символы ответа: {response.text[:120]!r}",
                what_to_do="Обычно это временный сбой. Система повторит позже.",
            ) from exc

        if "error" in payload:
            error = payload["error"]
            raise VkError(
                int(error.get("error_code", 0)),
                str(error.get("error_msg", "")),
                method=method,
            )
        return payload["response"]

    def _call_with_retries(self, client: httpx.Client, method: str, token: str, **params) -> Any:
        """Для идемпотентных вызовов. Публикация этим не пользуется."""
        last: Exception | None = None
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                return self._call(client, method, token, **params)
            except (httpx.HTTPError, ProviderError) as exc:
                if isinstance(exc, VkError) and exc.code not in (6, 9, 10):
                    raise
                last = exc
                log.warning(
                    "вызов ВК не удался, повторяю",
                    extra={"method": method, "attempt": attempt, "of": UPLOAD_ATTEMPTS},
                )
                self._sleep(UPLOAD_PAUSE_SEC)
        raise last  # type: ignore[misc]

    def _upload_one(self, client: httpx.Client, data: bytes, label: str) -> str:
        """Загружает картинку и возвращает строку вложения ``photo{owner}_{id}``.

        Пустой ``photo`` и не-JSON в ответе сервера загрузки — не ошибки, а повод
        начать заново с новым адресом: оба случая наблюдались на живом API.
        """
        for round_number in range(1, EMPTY_PHOTO_ROUNDS + 1):
            server = self._call_with_retries(
                client, "photos.getWallUploadServer", self.upload_token, group_id=self.group_id
            )

            raw: dict | None = None
            for attempt in range(1, UPLOAD_ATTEMPTS + 1):
                try:
                    response = client.post(
                        server["upload_url"],
                        files={"photo": (f"{label}.png", data, "image/png")},
                    )
                    response.raise_for_status()
                    raw = response.json()
                    break
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning(
                        "загрузка картинки не удалась, повторяю",
                        extra={"label": label, "attempt": attempt, "reason": type(exc).__name__},
                    )
                    self._sleep(UPLOAD_PAUSE_SEC)

            photo = (raw or {}).get("photo") or ""
            if not photo or photo == "[]":
                log.warning(
                    "сервер загрузки вернул пустой ответ, беру новый адрес",
                    extra={"label": label, "round": round_number},
                )
                continue

            saved = self._call_with_retries(
                client,
                "photos.saveWallPhoto",
                self.upload_token,
                group_id=self.group_id,
                photo=photo,
                server=raw["server"],
                hash=raw["hash"],
            )
            item = saved[0]
            return f"photo{item['owner_id']}_{item['id']}"

        raise ProviderError(
            f"Не удалось загрузить картинку {label} за {EMPTY_PHOTO_ROUNDS} заходов.",
            why="Сервер загрузки ВКонтакте каждый раз возвращал пустой ответ.",
            what_to_do="Обычно это временный сбой на их стороне. Система повторит позже.",
        )

    def _cleanup(self, client: httpx.Client, attachments: list[str]) -> None:
        """Убирает оригиналы из служебного альбома владельца ключа загрузки.

        При публикации от имени сообщества ВК копирует картинку в группу, а
        загруженный оригинал остаётся в невидимом альбоме личного профиля. Без
        уборки они копятся годами.

        Ошибки только логируются: пост уже опубликован, и падать из-за неубранного
        мусора было бы куда хуже самого мусора.
        """
        for attachment in attachments:
            try:
                owner_id, photo_id = attachment.removeprefix("photo").split("_")
                self._call(
                    client,
                    "photos.delete",
                    self.upload_token,
                    owner_id=owner_id,
                    photo_id=photo_id,
                )
            except (httpx.HTTPError, ProviderError, ValueError) as exc:
                log.warning(
                    "не удалось убрать оригинал картинки",
                    extra={"attachment": attachment, "reason": str(exc)},
                )

    def publish(self, post, assets) -> str:
        """Загружает картинки и публикует пост. Возвращает идентификатор записи."""
        message = "\n\n".join(part for part in (post.body, post.question) if part)
        guid = post.publish_guid or uuid.uuid4().hex

        with self._client() as client:
            attachments = [
                self._upload_one(client, self._read(asset), f"{asset.kind}_{asset.position}")
                for asset in assets
            ]

            params: dict[str, Any] = {
                "owner_id": -self.group_id,
                "from_group": 1,
                "message": message,
            }
            if attachments:
                params["attachments"] = ",".join(attachments)
            if guid:
                # Идентификатор операции: при повторе с тем же guid ВК вернёт уже
                # созданный пост вместо второго.
                params["guid"] = guid

            # Без повторов. Таймаут здесь не означает, что пост не создан.
            response = self._call(client, "wall.post", self.token, **params)
            post_id = response["post_id"]

            self._cleanup(client, attachments)

        log.info(
            "пост опубликован в ВК",
            extra={"post_id": post.id, "vk_post_id": post_id, "attachments": len(attachments)},
        )
        return f"{-self.group_id}_{post_id}"

    @staticmethod
    def _read(asset) -> bytes:
        from pathlib import Path

        if not asset.local_path or not Path(asset.local_path).is_file():
            raise ProviderError(
                f"Файл картинки не найден: {asset.local_path}",
                why="Картинка была сгенерирована, но файл исчез до публикации.",
                what_to_do="Перезапусти пост: factory post retry <id>.",
            )
        return Path(asset.local_path).read_bytes()

    def fetch_comments(self, external_id: str) -> list:
        """Появится на Этапе 6 вместе с воркером комментариев."""
        return []

    def reply(self, external_comment_id: str, text: str) -> None:
        raise ProviderError(
            "Ответы на комментарии ещё не реализованы.",
            why="Этот шаг появится на Этапе 6.",
            what_to_do="Ничего делать не нужно.",
        )
