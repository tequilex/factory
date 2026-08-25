"""Отправка в Telegram: медиагруппа, текст с кнопками, тревоги.

Сеть заблокирована conftest'ом, всё на httpx.MockTransport. Проверяется не
«вызов не упал», а что именно ушло на ту сторону: чужие ограничения Telegram
(1024 символа на подпись, 4096 на сообщение, кнопки только к обычному
сообщению) — это и есть причины, по которым код устроен именно так.
"""

import json
import urllib.parse

import httpx
import pytest

from factory.core.decisions import Decision
from factory.core.errors import ProviderError
from factory.core.retry import _is_retryable, _retry_after_sec
from factory.providers.notifiers.telegram import (
    MAX_MESSAGE_LENGTH,
    TelegramNotifier,
    parse_callback,
    review_keyboard,
)

TOKEN = "8685590879:AAтестовый"


class Recorder:
    """Ловит запросы и отвечает по сценарию."""

    def __init__(self, response=None):
        self.requests: list[httpx.Request] = []
        self.response = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if callable(self.response):
            return self.response(request)
        if self.response is not None:
            return self.response
        # sendMediaGroup отвечает списком сообщений, остальные методы — одним.
        # Отвечать одинаково значило бы проверять код против выдумки.
        if request.url.path.endswith("sendMediaGroup"):
            return httpx.Response(200, json={"ok": True, "result": [{"message_id": 100}]})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    def methods(self) -> list[str]:
        return [request.url.path.rsplit("/", 1)[-1] for request in self.requests]

    def form(self, method: str) -> dict:
        """Поля формы запроса к указанному методу, без файлов.

        Тип тела зависит от того, есть ли вложения: с файлами httpx шлёт
        multipart, без них — urlencoded. Разбираем оба, иначе половина проверок
        падала бы не по делу.
        """
        for request in self.requests:
            if not request.url.path.endswith(method):
                continue
            body = request.content.decode("utf-8", "replace")
            if "multipart/form-data" in request.headers.get("content-type", ""):
                return {
                    part.split('name="')[1].split('"')[0]: part.split("\r\n\r\n", 1)[1].rsplit(
                        "\r\n", 1
                    )[0]
                    for part in body.split("--")
                    if 'name="' in part and "\r\n\r\n" in part
                }
            return dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
        raise AssertionError(f"вызова {method} не было: {self.methods()}")


def notifier(recorder, monkeypatch) -> TelegramNotifier:
    transport = httpx.MockTransport(recorder.handler)
    monkeypatch.setattr(
        "factory.core.http.client_for",
        lambda *a, **kw: httpx.Client(transport=transport, headers=kw.get("headers") or {}),
    )
    return TelegramNotifier(token=TOKEN)


def send_review(client, *, project, title, body, warning, images, post_id=7):
    """Отправка на ревью целиком: альбом, затем текст ответом на него.

    В боевом коде этим управляет шаг — он решает, слать ли картинки повторно.
    Здесь склеено, чтобы проверять сами запросы к Telegram.
    """
    album_id = client.send_album(
        chat_id=123456789, caption=f"[{project}] {title}", images=images
    )
    return client.send_review_text(
        chat_id=123456789, project=project, title=title, body=body,
        warning=warning, post_id=post_id, reply_to=album_id,
    )


@pytest.fixture
def images(tmp_path):
    paths = []
    for number in range(3):
        path = tmp_path / f"scene{number}.jpg"
        path.write_bytes(b"\xff\xd8\xff" + bytes([number]) * 64)
        paths.append(str(path))
    return paths


class TestSendForReview:
    def test_the_album_goes_before_the_text(self, monkeypatch, images):
        """Порядок важен: сначала показать, потом спросить."""
        recorder = Recorder()
        recorder.response = lambda r: httpx.Response(
            200, json={"ok": True, "result": {"message_id": 42}}
        )

        send_review(notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок", body="Тело поста", warning=None, images=images)

        assert recorder.methods() == ["sendMediaGroup", "sendMessage"]

    def test_all_images_are_attached(self, monkeypatch, images):
        recorder = Recorder()

        send_review(notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок", body="Тело", warning=None, images=images)

        media = json.loads(recorder.form("sendMediaGroup")["media"])
        assert len(media) == len(images)
        assert [item["media"] for item in media] == [f"attach://file{n}" for n in range(3)]

    def test_the_album_is_captioned_with_the_project_and_title(self, monkeypatch, images):
        """Без подписи два сообщения читаются как «картинки, потом непонятный текст»."""
        recorder = Recorder()

        send_review(notifier(recorder, monkeypatch), project="vk_demo", title="Как выбрать шины", body="Тело", warning=None, images=images)

        media = json.loads(recorder.form("sendMediaGroup")["media"])
        assert media[0]["caption"] == "[vk_demo] Как выбрать шины"

    def test_only_the_first_photo_carries_the_caption(self, monkeypatch, images):
        """Telegram показывает подпись у первого вложения; на остальных это мусор."""
        recorder = Recorder()

        send_review(notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок", body="Тело", warning=None, images=images)

        media = json.loads(recorder.form("sendMediaGroup")["media"])
        assert all("caption" not in item for item in media[1:])

    def test_the_buttons_ride_with_the_text(self, monkeypatch, images):
        """Telegram не разрешает кнопки на медиагруппе — только на сообщении."""
        recorder = Recorder()

        send_review(notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок", body="Тело", warning=None, images=images)

        assert "reply_markup" not in recorder.form("sendMediaGroup")
        keyboard = json.loads(recorder.form("sendMessage")["reply_markup"])
        assert keyboard["inline_keyboard"]

    def test_the_project_is_named_in_the_message(self, monkeypatch, images):
        """При двух нишах иначе не понять, в какую группу уйдёт пост."""
        recorder = Recorder()

        send_review(notifier(recorder, monkeypatch), project="автоняша", title="Заголовок", body="Тело", warning=None, images=images)

        assert "автоняша" in recorder.form("sendMessage")["text"]

    def test_the_body_is_sent_whole(self, monkeypatch, images):
        """Подпись под альбомом — 1024 символа, пост в них не влезает."""
        body = "Ы" * 1400
        recorder = Recorder()

        send_review(notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок", body=body, warning=None, images=images)

        assert body in recorder.form("sendMessage")["text"]

    def test_a_factcheck_warning_is_shown(self, monkeypatch, images):
        recorder = Recorder()

        send_review(notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок", body="Тело", warning="фактчек не уверен в дате", images=images)

        assert "фактчек не уверен в дате" in recorder.form("sendMessage")["text"]

    def test_an_overlong_text_does_not_break_the_send(self, monkeypatch, images):
        """Лимит Telegram — 4096. Обрезать надо заметно, а не молча."""
        recorder = Recorder()

        send_review(notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок", body="Я" * 9000, warning=None, images=images)

        text = recorder.form("sendMessage")["text"]
        assert len(text) <= MAX_MESSAGE_LENGTH
        assert "обрезан" in text

    def test_the_message_id_comes_back(self, monkeypatch, images):
        """Без него нельзя снять кнопки после решения."""
        recorder = Recorder(
            lambda r: httpx.Response(
                200,
                json={"ok": True, "result": [{"message_id": 100}]}
                if r.url.path.endswith("sendMediaGroup")
                else {"ok": True, "result": {"message_id": 4242}},
            )
        )

        message = send_review(notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок", body="Тело", warning=None, images=images)

        assert message.chat_id == 123456789
        assert message.message_id == 4242

    def test_missing_files_do_not_stop_the_review(self, monkeypatch):
        """Пропавший файл — не повод не показать владельцу текст."""
        recorder = Recorder()

        send_review(
            notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок",
            body="Тело", warning=None, images=["/нет/такого.jpg"],
        )

        assert recorder.methods() == ["sendMessage"]


class TestErrors:
    def test_a_bot_that_was_never_started_explains_itself(self, monkeypatch, images):
        """Самая частая ошибка первого запуска, и по коду 403 она непонятна."""
        recorder = Recorder(
            httpx.Response(403, json={"ok": False, "error_code": 403,
                                      "description": "Forbidden: bot can't initiate conversation"})
        )

        with pytest.raises(ProviderError) as excinfo:
            notifier(recorder, monkeypatch).alert(chat_id=123456789, text="привет")

        assert "/start" in str(excinfo.value)

    def test_a_bad_token_names_the_secret_from_the_config(self, monkeypatch):
        """Имя переменной настраиваемое: выдуманное указало бы не на ту строку."""
        recorder = Recorder(
            httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
        )
        transport = httpx.MockTransport(recorder.handler)
        monkeypatch.setattr(
            "factory.core.http.client_for",
            lambda *a, **kw: httpx.Client(transport=transport, headers=kw.get("headers") or {}),
        )
        custom = TelegramNotifier(token=TOKEN, token_env="BOT_TOKEN_ВТОРОЙ")

        with pytest.raises(ProviderError) as excinfo:
            custom.alert(chat_id=123456789, text="привет")

        assert "BOT_TOKEN_ВТОРОЙ" in str(excinfo.value)

    def test_a_wrong_chat_id_points_at_the_config(self, monkeypatch):
        recorder = Recorder(
            httpx.Response(400, json={"ok": False, "error_code": 400,
                                      "description": "Bad Request: chat not found"})
        )

        with pytest.raises(ProviderError) as excinfo:
            notifier(recorder, monkeypatch).alert(chat_id=1, text="привет")

        message = str(excinfo.value)
        assert "telegram.chat_id" in message
        assert "@userinfobot" in message

    def test_rate_limiting_is_retryable_and_carries_the_wait(self, monkeypatch):
        recorder = Recorder(
            httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many",
                                      "parameters": {"retry_after": 12}})
        )

        with pytest.raises(ProviderError) as excinfo:
            notifier(recorder, monkeypatch).alert(chat_id=123456789, text="привет")

        assert _is_retryable(excinfo.value)
        assert _retry_after_sec(excinfo.value) == 12.0

    def test_a_bad_token_is_not_retried(self, monkeypatch):
        """Повторять неверный токен бессмысленно — он не исправится сам."""
        recorder = Recorder(
            httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
        )

        with pytest.raises(ProviderError) as excinfo:
            notifier(recorder, monkeypatch).alert(chat_id=123456789, text="привет")

        assert not _is_retryable(excinfo.value)


class TestKeyboard:
    def test_every_decision_has_a_button(self, ):
        keyboard = review_keyboard(7)
        flat = [button for row in keyboard["inline_keyboard"] for button in row]

        assert len(flat) == len(list(Decision))

    def test_a_button_carries_the_post_number(self):
        """Бот перезапускается, а сообщение живёт в переписке неделями.

        Номер поста обязан быть в самой кнопке, иначе после перезапуска
        нажатие некуда применить.
        """
        keyboard = review_keyboard(7)
        flat = [button for row in keyboard["inline_keyboard"] for button in row]

        assert all(button["callback_data"].startswith("r:7:") for button in flat)

    def test_callback_data_fits_the_telegram_limit(self):
        """Telegram обрезает callback_data длиннее 64 байт — кнопка перестаёт работать."""
        keyboard = review_keyboard(9_999_999)
        flat = [button for row in keyboard["inline_keyboard"] for button in row]

        assert all(len(button["callback_data"].encode()) <= 64 for button in flat)

    @pytest.mark.parametrize("decision", list(Decision))
    def test_every_button_parses_back(self, decision):
        assert parse_callback(f"r:7:{decision.value}") == (7, decision)

    @pytest.mark.parametrize("data", ["", "мусор", "r:7", "r:семь:ok", "r:7:неизвестно", "x:7:ok"])
    def test_foreign_or_broken_data_is_refused(self, data):
        """Чужая кнопка не должна применяться наугад."""
        assert parse_callback(data) is None


class TestConnectFailuresAreRetried:
    """Сбой установки соединения — единственный, который можно повторять.

    Соединение не состоялось, значит на той стороне ничего не появилось.
    Таймаут чтения так повторять нельзя: запрос уже ушёл, и владелец получит
    второй альбом — именно так он однажды и получил один пост трижды.
    """

    def notifier_with(self, monkeypatch, handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            "factory.core.http.client_for",
            lambda *a, **kw: httpx.Client(transport=transport, headers=kw.get("headers") or {}),
        )
        return TelegramNotifier(token=TOKEN, sleep=lambda _: None)

    def test_a_handshake_timeout_is_retried(self, monkeypatch):
        attempts = {"n": 0}

        def flaky(request):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectTimeout("handshake timed out")
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        self.notifier_with(monkeypatch, flaky).alert(chat_id=123456789, text="привет")

        assert attempts["n"] == 3

    def test_a_refused_connection_is_retried(self, monkeypatch):
        attempts = {"n": 0}

        def flaky(request):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        self.notifier_with(monkeypatch, flaky).alert(chat_id=123456789, text="привет")

        assert attempts["n"] == 2

    def test_a_read_timeout_is_never_retried(self, monkeypatch):
        """Запрос уже ушёл: повтор прислал бы владельцу второй альбом."""
        attempts = {"n": 0}

        def hangs(request):
            attempts["n"] += 1
            raise httpx.ReadTimeout("не дождались ответа")

        with pytest.raises(ProviderError) as excinfo:
            self.notifier_with(monkeypatch, hangs).alert(chat_id=123456789, text="привет")

        assert attempts["n"] == 1, "повторили запрос, который мог дойти"
        assert excinfo.value.delivered_unknown is True

    def test_a_failure_to_connect_is_marked_as_never_sent(self, monkeypatch):
        """По этому признаку шаг решает, можно ли слать альбом заново."""

        def dead(request):
            raise httpx.ConnectTimeout("handshake timed out")

        with pytest.raises(ProviderError) as excinfo:
            self.notifier_with(monkeypatch, dead).alert(chat_id=123456789, text="привет")

        assert excinfo.value.delivered_unknown is False

    def test_giving_up_explains_the_proxy(self, monkeypatch):
        def dead(request):
            raise httpx.ConnectTimeout("сеть недоступна")

        with pytest.raises(ProviderError) as excinfo:
            self.notifier_with(monkeypatch, dead).alert(chat_id=123456789, text="привет")

        message = str(excinfo.value)
        assert "telegram.proxy_env" in message

    def test_the_album_is_not_sent_twice_on_a_connect_retry(self, monkeypatch, images):
        """Повтор соединения не должен превращаться в два альбома."""
        seen = []

        def flaky(request):
            seen.append(request.url.path.rsplit("/", 1)[-1])
            if len(seen) == 1:
                raise httpx.ConnectTimeout("handshake timed out")
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        send_review(
            self.notifier_with(monkeypatch, flaky), project="vk_demo", title="Заголовок",
            body="Тело", warning=None, images=images,
        )

        assert seen.count("sendMediaGroup") == 2, "первый заход не состоялся, второй — доставка"
        assert seen.count("sendMessage") == 1


class TestTextRepliesToTheAlbum:
    """Связь между картинками и текстом показывает Telegram, а не память.

    При сбоях порядок отправки сбивается: на живом прогоне владелец увидел
    альбом одного поста и следом текст другого, и решил, что система перепутала.
    """

    def test_the_text_replies_to_the_album_message(self, monkeypatch, images):
        recorder = Recorder()

        send_review(
            notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок",
            body="Тело", warning=None, images=images,
        )

        assert recorder.form("sendMessage")["reply_to_message_id"] == "100"

    def test_a_deleted_album_does_not_block_the_text(self, monkeypatch, images):
        """Владелец мог удалить картинки — кнопки всё равно должны прийти."""
        recorder = Recorder()

        send_review(
            notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок",
            body="Тело", warning=None, images=images,
        )

        assert recorder.form("sendMessage")["allow_sending_without_reply"] == "true"

    def test_without_an_album_nothing_is_replied_to(self, monkeypatch):
        recorder = Recorder()

        send_review(
            notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок",
            body="Тело", warning=None, images=[],
        )

        assert "reply_to_message_id" not in recorder.form("sendMessage")

    def test_an_unexpected_answer_does_not_lose_the_album(self, monkeypatch, images):
        """Не разобрали номер — текст уйдёт отдельно, но картинки не пропадут."""
        recorder = Recorder(
            lambda r: httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
        )

        message = send_review(
            notifier(recorder, monkeypatch), project="vk_demo", title="Заголовок",
            body="Тело", warning=None, images=images,
        )

        assert message.message_id == 7
        assert "sendMediaGroup" in recorder.methods()
