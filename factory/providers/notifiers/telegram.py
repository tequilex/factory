"""Отправка в Telegram обычным HTTP, без aiogram.

Библиотека для ботов асинхронная, а воркер — нет. Тащить асинхронность в
синхронный тик ради четырёх запросов значит получить зависания, которые не
воспроизводятся. Здесь только исходящие вызовы, они прекрасно делаются тем же
``core/http.py``, что и ВК. Приём нажатий — забота бота, отдельного процесса.

Ограничения Telegram, из-за которых код такой, какой есть:

* подпись под медиагруппой — 1024 символа, обычное сообщение — 4096. Пост в
  1400 символов не влезает в подпись, поэтому текст уходит отдельно;
* кнопки нельзя прицепить к медиагруппе, только к обычному сообщению. Значит
  клавиатура едет с текстом, и именно его id надо запомнить;
* бот не может написать первым. Пока владелец не отправил боту ``/start``,
  любая отправка отвечает 403.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from factory.core import http
from factory.core.decisions import LABEL, Decision
from factory.core.errors import ProviderError
from factory.core.logging import get_logger
from factory.providers.base import ReviewMessage

log = get_logger(__name__)

API_BASE = "https://api.telegram.org"

# Лимиты Telegram. Числа их, не наши: менять нельзя, можно только уложиться.
MAX_MESSAGE_LENGTH = 4096
# Подпись под медиагруппой. Именно из-за неё текст поста едет отдельным
# сообщением: 1400 символов сюда не помещаются.
MAX_CAPTION_LENGTH = 1024
MAX_MEDIA_IN_GROUP = 10

# Сколько раз пробовать, если не удалось даже установить соединение. Такой сбой
# отличается от прочих тем, что запрос заведомо не ушёл: повторить его безопасно,
# дубля не будет. Сеть до api.telegram.org отвечает неровно, и без этого каждая
# третья отправка откладывалась на десять минут.
CONNECT_ATTEMPTS = 3
CONNECT_PAUSE_SEC = 2.0

# Клавиатура ревью. Порядок и разбивка по строкам — как в SPEC.md.
KEYBOARD_ROWS: tuple[tuple[Decision, ...], ...] = (
    (Decision.APPROVE, Decision.IMAGES),
    (Decision.SCENES, Decision.TEXT),
    (Decision.TRASH,),
)
# Decision.CANCEL сюда не входит: он появляется отдельной кнопкой уже после
# одобрения, когда остальные решения неприменимы.

ICON: dict[Decision, str] = {
    Decision.APPROVE: "✅",
    Decision.TRASH_TOPIC: "🚫",
    Decision.RETRY: "🔧",
    Decision.CANCEL: "↩️",
    Decision.IMAGES: "🔄",
    Decision.SCENES: "🎲",
    Decision.TEXT: "✏️",
    Decision.TRASH: "🗑",
}


#: Псевдодействия в callback_data. Решениями не являются: первое открывает
#: переспрос, второе от него отказывается.
ASK_TRASH = "ask"
KEEP = "keep"


def review_keyboard(post_id: int, version: int = 1) -> dict:
    """Клавиатура под постом. ``callback_data`` несёт пост, решение и вариант.

    Всё нужное — в самой кнопке, а не в памяти бота: бот перезапускается, а
    сообщения живут в переписке неделями. Нажатие на вариант, присланный три
    дня назад, обязано сработать.
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{ICON[item]} {_label_for(item, version)}",
                    # «В мусор» не выбрасывает сразу: сначала переспрос, что
                    # именно выбрасываем. Необратимое действие не должно
                    # срабатывать от одного нажатия.
                    "callback_data": (
                        f"r:{post_id}:{ASK_TRASH}:{version}"
                        if item is Decision.TRASH
                        else f"r:{post_id}:{item.value}:{version}"
                    ),
                }
                for item in row
            ]
            for row in KEYBOARD_ROWS
        ]
    }


def _label_for(decision: Decision, version: int) -> str:
    """У одобрения подпись зависит от того, есть ли выбор.

    «Опубликовать этот» на единственном варианте звучит так, будто где-то есть
    другие. «Опубликовать» на третьем из пяти не говорит, какой именно.
    """
    if decision is Decision.APPROVE and version > 1:
        return "Опубликовать этот"
    return LABEL[decision]


def cancel_keyboard(post_id: int, version: int = 1) -> dict:
    """Единственная кнопка под одобренным постом — «передумал».

    Убирать клавиатуру совсем нельзя: пост одобрен, но ещё не вышел, и до
    ближайшего слота владелец вправе передумать. Без кнопки единственный путь
    назад — командная строка, которой у него нет.
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{ICON[Decision.CANCEL]} {LABEL[Decision.CANCEL]}",
                    # Номер варианта обязан ехать и здесь: без него отмена
                    # возвращает клавиатуру первого варианта на сообщение
                    # третьего, и следующее «Опубликовать» уходит не тем постом.
                    "callback_data": f"r:{post_id}:{Decision.CANCEL.value}:{version}",
                }
            ]
        ]
    }


def variant_keyboard(post_id: int, version: int) -> dict:
    """Единственная кнопка под вариантом, который отложили в сторону.

    Остальные решения под ним больше не значат ничего: пост уже переделывается,
    и «Текст заново» на старом сообщении завёл бы третий вариант вместо выбора
    между первыми двумя. А вот опубликовать этот вариант — ровно то, ради чего
    он сохранён.
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{ICON[Decision.APPROVE]} Опубликовать этот вариант",
                    "callback_data": f"r:{post_id}:{Decision.APPROVE.value}:{version}",
                }
            ]
        ]
    }


def trash_keyboard(post_id: int, version: int) -> dict:
    """Что именно выбрасываем.

    «В мусор» звучит как «выбросить всё», а выбрасывался только пост: тема
    возвращалась в очередь, и по ней тут же писался такой же. Владелец при
    этом ничего не выбирал — теперь выбирает.

    Заодно это переспрос перед необратимым: раньше пост со всеми вариантами
    терялся от одного случайного нажатия.
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🗑 Только этот пост",
                    "callback_data": f"r:{post_id}:{Decision.TRASH.value}:{version}",
                }
            ],
            [
                {
                    "text": "🚫 Пост и тему",
                    "callback_data": f"r:{post_id}:{Decision.TRASH_TOPIC.value}:{version}",
                }
            ],
            [{"text": "← Назад", "callback_data": f"r:{post_id}:{KEEP}:{version}"}],
        ]
    }


def retry_keyboard(post_id: int) -> dict:
    """Кнопка под сообщением о сломанном посте.

    До неё в тексте тревоги стояла команда для терминала — то есть владельцу,
    который живёт в телефоне, предлагалось сделать невозможное.
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{ICON[Decision.RETRY]} {LABEL[Decision.RETRY]}",
                    "callback_data": f"r:{post_id}:{Decision.RETRY.value}",
                }
            ]
        ]
    }


def parse_callback(data: str) -> tuple[int, Decision, int | None] | None:
    """Разобрать ``callback_data``. ``None`` — кнопка не наша или испорчена.

    Третье поле — номер варианта. Его может не быть: кнопки, отправленные до
    появления вариантов, продолжают работать и означают «текущий».
    """
    parts = data.split(":")
    if len(parts) not in (3, 4) or parts[0] != "r":
        return None
    try:
        post_id = int(parts[1])
        decision = Decision(parts[2])
        version = int(parts[3]) if len(parts) == 4 else None
    except ValueError:
        return None
    return post_id, decision, version


def _advice(code: int, description: str, token_env: str = "TELEGRAM_BOT_TOKEN") -> str:
    if code == 401:
        return (
            f"Токен бота не принят. Проверь строку {token_env} в файле "
            "секретов — возможно, бот удалён или токен перевыпущен у @BotFather."
        )
    if code == 403:
        return (
            "Бот не может написать первым — так устроен Telegram. Открой бота "
            "и отправь ему /start, после этого сообщения будут доходить."
        )
    if code == 400 and "chat not found" in description.lower():
        return (
            "Чат не найден. Проверь telegram.chat_id в конфиге проекта: это "
            "число выдаёт бот @userinfobot."
        )
    if code == 429:
        return "Слишком часто. Система подождёт и повторит сама."
    return f"Ответ Telegram: {description[:200]}"


class TelegramNotifier:
    """Уведомления владельцу через Bot API."""

    name = "telegram"

    def __init__(
        self,
        *,
        token: str,
        token_env: str = "TELEGRAM_BOT_TOKEN",
        proxy_env: str | None = None,
        sleep=time.sleep,
    ) -> None:
        self.token = token
        self.token_env = token_env
        self._sleep = sleep
        self.proxy_env = proxy_env
        self.calls = 0

    def _client(self) -> httpx.Client:
        return http.client_for("telegram", proxy_env=self.proxy_env)

    def _call(self, method: str, *, data: dict, files: dict | None = None) -> Any:
        """Один вызов Bot API. Ошибку превращает в понятную человеку."""
        self.calls += 1
        response = self._send(method, data=data, files=files)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"Telegram вернул не-JSON в ответ на {method}.",
                why=f"Код {response.status_code}, начало ответа: {response.text[:120]!r}",
                what_to_do="Обычно это временный сбой. Система повторит позже.",
                status_code=response.status_code,
            ) from exc

        if not payload.get("ok"):
            code = int(payload.get("error_code", response.status_code))
            description = str(payload.get("description", ""))
            raise ProviderError(
                f"Telegram отказал в {method}.",
                why=f"Код {code}: {description}",
                what_to_do=_advice(code, description, self.token_env),
                status_code=code,
                retry_after=_retry_after(payload),
            )

        return payload.get("result")

    def _send(self, method: str, *, data: dict, files: dict | None) -> httpx.Response:
        """Запрос с повтором только на сбоях установки соединения.

        Повторяются исключительно ``ConnectError`` и ``ConnectTimeout``: при них
        соединение не состоялось, значит на той стороне ничего не появилось.
        Таймаут чтения не повторяется никогда — там запрос уже ушёл, и повтор
        прислал бы владельцу второй альбом. Ровно так он однажды и получил один
        и тот же пост трижды.
        """
        url = f"{API_BASE}/bot{self.token}/{method}"
        last: Exception | None = None

        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                with self._client() as client:
                    return client.post(url, data=data, files=files)
            except httpx.TimeoutException as exc:
                if not isinstance(exc, httpx.ConnectTimeout):
                    # Запрос ушёл, ответа нет. Что произошло на той стороне,
                    # мы не знаем — и повторять это нельзя.
                    raise ProviderError(
                        f"Telegram не ответил на {method} вовремя.",
                        why=str(exc),
                        what_to_do="Система разберётся сама на следующем проходе.",
                        delivered_unknown=True,
                    ) from exc
                last = exc
                log.warning(
                    "не удалось соединиться с Telegram, повторяю",
                    extra={"method": method, "attempt": attempt, "of": CONNECT_ATTEMPTS},
                )
                if attempt < CONNECT_ATTEMPTS:
                    self._sleep(CONNECT_PAUSE_SEC)
                continue
            except httpx.ConnectError as exc:
                last = exc
                log.warning(
                    "не удалось соединиться с Telegram, повторяю",
                    extra={"method": method, "attempt": attempt, "of": CONNECT_ATTEMPTS},
                )
                if attempt < CONNECT_ATTEMPTS:
                    self._sleep(CONNECT_PAUSE_SEC)

        assert last is not None
        raise ProviderError(
            "Не удалось соединиться с Telegram.",
            why=f"{CONNECT_ATTEMPTS} попытки подряд: {last}",
            what_to_do=(
                "Обычно это временный сбой сети — система повторит позже. "
                "Если повторяется постоянно, укажи telegram.proxy_env в конфиге "
                "проекта: из некоторых сетей api.telegram.org недоступен."
            ),
            # Соединение не состоялось — на той стороне ничего не появилось.
            delivered_unknown=False,
        ) from last

    def send_album(self, *, chat_id: int, caption: str, images: list[str]) -> int | None:
        """Медиагруппа: файлы вложениями, подпись — у первого.

        Возвращает номер первого сообщения, чтобы текст ушёл ответом на него.
        """
        existing = [path for path in images if Path(path).is_file()][:MAX_MEDIA_IN_GROUP]
        if not existing:
            return None

        media, files = [], {}
        for number, path in enumerate(existing):
            key = f"file{number}"
            item: dict[str, Any] = {"type": "photo", "media": f"attach://{key}"}
            if number == 0 and caption:
                # Telegram показывает подпись только у первого вложения.
                item["caption"] = caption[:MAX_CAPTION_LENGTH]
            media.append(item)
            files[key] = (Path(path).name, Path(path).read_bytes())

        sent = self._call(
            "sendMediaGroup", data={"chat_id": chat_id, "media": _json(media)}, files=files
        )
        # sendMediaGroup отвечает списком сообщений. Разбор защищён: без номера
        # сообщения текст просто уйдёт не ответом, а отдельно — это хуже, но не
        # повод терять уже отправленные картинки.
        if isinstance(sent, list) and sent:
            return int(sent[0].get("message_id", 0)) or None
        return None

    def send_review_text(
        self,
        *,
        chat_id: int,
        project: str,
        title: str,
        body: str,
        warning: str | None,
        post_id: int,
        reply_to: int | None = None,
        version: int = 1,
        total: int = 1,
    ) -> ReviewMessage:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": _review_text(project, title, body, warning, version, total),
            "reply_markup": _json(review_keyboard(post_id, version)),
            "disable_web_page_preview": True,
        }
        if reply_to is not None:
            # Ответом на альбом: иначе при сбившемся порядке владелец видит
            # картинки одного поста рядом с текстом другого.
            data["reply_to_message_id"] = reply_to
            # Альбом мог быть удалён вручную — сообщение всё равно должно уйти.
            data["allow_sending_without_reply"] = True

        message = self._call("sendMessage", data=data)
        return ReviewMessage(chat_id=chat_id, message_id=int(message["message_id"]))

    def send_waiting(self, *, chat_id: int, text: str) -> int | None:
        message = self._call("sendMessage", data={"chat_id": chat_id, "text": _cut(text)})
        return int(message["message_id"]) if message else None

    def forget(self, *, chat_id: int, message_id: int) -> None:
        """Убрать сообщение. Отказ Telegram проглатывается намеренно.

        Сообщение могли удалить руками, оно могло устареть — уборка не повод
        обрывать работу, ради которой всё затевалось.
        """
        try:
            self._call(
                "deleteMessage", data={"chat_id": chat_id, "message_id": message_id}
            )
        except ProviderError as exc:
            log.info("не удалось убрать сообщение", extra={"reason": str(exc)})

    def finish_review(self, *, chat_id: int, message_id: int, text: str) -> None:
        """Пост вышел: снять кнопку отмены и дать ссылку.

        Кнопка «Отменить публикацию» под уже вышедшим постом — обещание,
        которого система выполнить не может: удалять записи в группе она не
        умеет и не должна.
        """
        self._call(
            "editMessageReplyMarkup",
            data={"chat_id": chat_id, "message_id": message_id, "reply_markup": _json({})},
        )
        self._call(
            "sendMessage",
            data={
                "chat_id": chat_id,
                "text": _cut(text),
                "reply_to_message_id": message_id,
                "allow_sending_without_reply": True,
                "disable_web_page_preview": True,
            },
        )

    def alert(self, *, chat_id: int, text: str, fix_post_id: int | None = None) -> None:
        data: dict[str, Any] = {"chat_id": chat_id, "text": _cut(text)}
        if fix_post_id is not None:
            data["reply_markup"] = _json(retry_keyboard(fix_post_id))
        self._call("sendMessage", data=data)

def _retry_after(payload: dict) -> float | None:
    parameters = payload.get("parameters") or {}
    value = parameters.get("retry_after")
    return float(value) if isinstance(value, (int, float)) else None


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _cut(text: str) -> str:
    """Обрезать до лимита Telegram, не потеряв конец молча."""
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    tail = "\n\n[…текст обрезан, целиком — factory post show]"
    return text[: MAX_MESSAGE_LENGTH - len(tail)] + tail


def _review_text(
    project: str, title: str, body: str, warning: str | None,
    version: int = 1, total: int = 1,
) -> str:
    """Что владелец видит под картинками.

    Имя проекта — первой строкой: при двух нишах иначе не понять, в какую группу
    уйдёт пост, а кнопка «Опубликовать» выглядит одинаково. Номер варианта —
    там же: без него непонятно, сколько ещё вариантов выше в переписке.
    """
    head = f"[{project}] {title}"
    if total > 1:
        head = f"[{project}] Вариант {version} из {total} · {title}"
    parts = [head, "", body]
    if warning:
        parts += ["", f"⚠️ {warning}"]
    return _cut("\n".join(parts))


def extract_vk_token(text: str) -> str | None:
    """Вынуть ключ ВК из того, что прислал владелец.

    Принимается и весь адрес после входа, и один ключ: адрес приходит из
    браузера на телефоне, и просить человека аккуратно выделить подстроку между
    двумя разделителями — верный способ получить ключ, обрезанный на символ.

    Ключ ВК выглядит как ``vk1.a.<длинная строка>``. Проверка по виду, а не
    просто «что-то после access_token=», отсекает случайно присланную ссылку.
    """
    import re

    match = re.search(r"access_token=([A-Za-z0-9._-]+)", text)
    candidate = match.group(1) if match else text.strip()

    if not re.fullmatch(r"vk\d+\.[A-Za-z0-9._-]{20,}", candidate):
        return None
    return candidate
