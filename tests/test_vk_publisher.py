"""Боевой публикатор ВК — целиком на моках.

Настоящая сеть в тестах запрещена предохранителем из conftest. Здесь проверяется
то, что на живом API проверить нельзя: порядок вызовов, каким ключом какой вызов
сделан, поведение при сбоях и — главное — что публикация не повторяется.
"""

from urllib.parse import parse_qs

import httpx
import pytest

from factory.core.errors import ProviderError
from factory.providers.publishers.vk import VkError, VkPublisher

GROUP = 111222333
UPLOAD_URL = "https://pu.vk.com/c123/upload.php?act=do_add"


class Asset:
    def __init__(self, kind, position, path):
        self.kind = kind
        self.position = position
        self.local_path = str(path)


class Post:
    id = 7
    body = "Текст поста"
    question = "А у вас так же?"
    publish_guid = "заранее-сохранённый-guid"


@pytest.fixture
def images(tmp_path):
    """Обложка и одна доп. картинка на диске."""
    files = []
    for kind, position in [("cover", 0), ("inline", 1)]:
        path = tmp_path / f"{kind}_{position}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + kind.encode())
        files.append(Asset(kind, position, path))
    return files


class Recorder:
    """Записывает все вызовы и отвечает по сценарию."""

    def __init__(self, script=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.script = script or {}
        self.rounds: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if url.startswith(UPLOAD_URL.split("?")[0]):
            self.calls.append(("upload", "", {}))
            return self._upload_response()

        method = url.rsplit("/", 1)[-1].split("?")[0]
        # Тело приходит в URL-кодировке: без разбора кириллица превращается в
        # проценты, и сравнение с ожидаемым значением всегда ложно.
        body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        token = body.get("access_token", "")
        self.calls.append((method, token, body))

        scripted = self.script.get(method)
        if callable(scripted):
            return scripted(self, body)
        if scripted is not None:
            return scripted

        return httpx.Response(200, json=self._default(method))

    def _upload_response(self) -> httpx.Response:
        count = self.rounds.get("upload", 0)
        self.rounds["upload"] = count + 1
        return httpx.Response(
            200, json={"server": 1, "photo": '[{"photo":"x"}]', "hash": "h"}
        )

    def _default(self, method: str) -> dict:
        if method == "photos.getWallUploadServer":
            return {"response": {"upload_url": UPLOAD_URL}}
        if method == "photos.saveWallPhoto":
            index = self.rounds.get("saved", 0)
            self.rounds["saved"] = index + 1
            return {"response": [{"owner_id": 210741654, "id": 1000 + index}]}
        if method == "wall.post":
            return {"response": {"post_id": 42}}
        if method == "photos.delete":
            return {"response": 1}
        return {"response": {}}

    def methods(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def token_for(self, method: str) -> str:
        return next(token for name, token, _ in self.calls if name == method)

    def params_for(self, method: str) -> dict:
        return next(body for name, _, body in self.calls if name == method)


def publisher(recorder, monkeypatch, **kwargs):
    transport = httpx.MockTransport(recorder.handler)
    monkeypatch.setattr(
        "factory.core.http.client_for",
        lambda *a, **kw: httpx.Client(transport=transport, base_url=""),
    )
    return VkPublisher(
        group_id=GROUP,
        token="ключ-сообщества",
        upload_token="ключ-пользователя",
        sleep=lambda _: None,
        **kwargs,
    )


def error(code: int, message: str = "beda") -> httpx.Response:
    return httpx.Response(200, json={"error": {"error_code": code, "error_msg": message}})


class TestHappyPath:
    def test_calls_go_in_the_documented_order(self, images, monkeypatch):
        recorder = Recorder()

        publisher(recorder, monkeypatch).publish(Post(), images)

        upload_sequence = [m for m in recorder.methods() if m != "photos.delete"]
        assert upload_sequence == [
            "photos.getWallUploadServer",
            "upload",
            "photos.saveWallPhoto",
            "photos.getWallUploadServer",
            "upload",
            "photos.saveWallPhoto",
            "wall.post",
        ]

    def test_upload_uses_the_user_key_and_publishing_the_community_one(
        self, images, monkeypatch
    ):
        """Ключи не взаимозаменяемы: перепутать — значит получить ошибку 27 или 15."""
        recorder = Recorder()

        publisher(recorder, monkeypatch).publish(Post(), images)

        assert recorder.token_for("photos.getWallUploadServer") == "ключ-пользователя"
        assert recorder.token_for("photos.saveWallPhoto") == "ключ-пользователя"
        assert recorder.token_for("wall.post") == "ключ-сообщества"

    def test_posts_from_the_group_not_from_a_person(self, images, monkeypatch):
        recorder = Recorder()

        publisher(recorder, monkeypatch).publish(Post(), images)

        params = recorder.params_for("wall.post")
        assert params["owner_id"] == f"-{GROUP}"
        assert params["from_group"] == "1"

    def test_returns_the_identifier_of_the_created_post(self, images, monkeypatch):
        recorder = Recorder()

        result = publisher(recorder, monkeypatch).publish(Post(), images)

        assert result == f"-{GROUP}_42"

    def test_all_images_are_attached(self, images, monkeypatch):
        recorder = Recorder()

        publisher(recorder, monkeypatch).publish(Post(), images)

        attachments = recorder.params_for("wall.post")["attachments"].split(",")
        assert len(attachments) == 2

    def test_message_carries_body_and_question(self, images, monkeypatch):
        recorder = Recorder()

        publisher(recorder, monkeypatch).publish(Post(), images)

        message = recorder.params_for("wall.post")["message"]
        assert "Текст поста" in message
        assert "А у вас так же?" in message


class TestIdempotency:
    def test_guid_from_the_database_is_sent(self, images, monkeypatch):
        """guid сохраняется до вызова: при повторе ВК вернёт уже созданный пост."""
        recorder = Recorder()

        publisher(recorder, monkeypatch).publish(Post(), images)

        assert recorder.params_for("wall.post")["guid"] == Post.publish_guid

    def test_guid_is_generated_when_the_post_has_none(self, images, monkeypatch):
        recorder = Recorder()
        post = Post()
        post.publish_guid = None

        publisher(recorder, monkeypatch).publish(post, images)

        assert recorder.params_for("wall.post")["guid"]


class TestRetries:
    def test_publishing_is_never_repeated(self, images, monkeypatch):
        """Главное правило шага: таймаут не значит «пост не создан»."""
        attempts = {"n": 0}

        def timing_out(recorder, body):
            attempts["n"] += 1
            raise httpx.ReadTimeout("ответ не доехал")

        recorder = Recorder({"wall.post": timing_out})

        with pytest.raises(httpx.ReadTimeout):
            publisher(recorder, monkeypatch).publish(Post(), images)

        assert attempts["n"] == 1, f"публикация повторена {attempts['n']} раз"

    def test_getting_an_upload_server_is_repeated(self, images, monkeypatch):
        """Идемпотентный вызов — повторять безопасно и нужно: сеть до ВК рвётся."""
        attempts = {"n": 0}

        def flaky(recorder, body):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectTimeout("рукопожатие не прошло")
            return httpx.Response(200, json={"response": {"upload_url": UPLOAD_URL}})

        recorder = Recorder({"photos.getWallUploadServer": flaky})

        publisher(recorder, monkeypatch).publish(Post(), images)

        assert attempts["n"] >= 3

    def test_expired_key_is_not_retried(self, images, monkeypatch):
        """Ошибка 5 не пройдёт от повторов — только зря потратим время."""
        attempts = {"n": 0}

        def expired(recorder, body):
            attempts["n"] += 1
            return error(5, "User authorization failed")

        recorder = Recorder({"photos.getWallUploadServer": expired})

        with pytest.raises(ProviderError):
            publisher(recorder, monkeypatch).publish(Post(), images)

        assert attempts["n"] == 1


class TestUploadServerQuirks:
    def test_empty_photo_field_triggers_a_fresh_upload_url(self, images, monkeypatch):
        """Наблюдалось живьём: успешный ответ с пустым photo. Помогает новый адрес."""
        rounds = {"n": 0}

        class Quirky(Recorder):
            def _upload_response(self):
                rounds["n"] += 1
                if rounds["n"] == 1:
                    return httpx.Response(200, json={"server": 1, "photo": "", "hash": "h"})
                return httpx.Response(
                    200, json={"server": 1, "photo": '[{"photo":"x"}]', "hash": "h"}
                )

        recorder = Quirky()

        publisher(recorder, monkeypatch).publish(Post(), images)

        assert recorder.methods().count("photos.getWallUploadServer") == 3

    def test_non_json_from_the_upload_server_is_retried(self, images, monkeypatch):
        """Тоже наблюдалось живьём: сервер вернул страницу вместо JSON."""
        rounds = {"n": 0}

        class Quirky(Recorder):
            def _upload_response(self):
                rounds["n"] += 1
                if rounds["n"] == 1:
                    return httpx.Response(200, text="<html>ошибка</html>")
                return httpx.Response(
                    200, json={"server": 1, "photo": '[{"photo":"x"}]', "hash": "h"}
                )

        recorder = Quirky()

        publisher(recorder, monkeypatch).publish(Post(), images)

        assert recorder.methods().count("upload") >= 3

    def test_giving_up_after_too_many_empty_answers(self, images, monkeypatch):
        class Broken(Recorder):
            def _upload_response(self):
                return httpx.Response(200, json={"server": 1, "photo": "", "hash": "h"})

        recorder = Broken()

        with pytest.raises(ProviderError, match="пустой ответ"):
            publisher(recorder, monkeypatch).publish(Post(), images)


class TestCleanup:
    def test_originals_are_removed_after_publishing(self, images, monkeypatch):
        """Иначе оригиналы копятся в невидимом альбоме владельца ключа."""
        recorder = Recorder()

        publisher(recorder, monkeypatch).publish(Post(), images)

        assert recorder.methods().count("photos.delete") == 2

    def test_cleanup_runs_after_publishing_not_before(self, images, monkeypatch):
        recorder = Recorder()

        publisher(recorder, monkeypatch).publish(Post(), images)

        methods = recorder.methods()
        assert methods.index("wall.post") < methods.index("photos.delete")

    def test_failed_cleanup_does_not_undo_the_publication(self, images, monkeypatch):
        """Пост уже в группе. Падать из-за неубранного мусора нельзя."""
        recorder = Recorder({"photos.delete": error(15, "Access denied")})

        result = publisher(recorder, monkeypatch).publish(Post(), images)

        assert result == f"-{GROUP}_42"

    def test_network_failure_during_cleanup_does_not_undo_the_publication(
        self, images, monkeypatch
    ):
        """Обрыв связи на уборке — не ошибка ВК, и ловится другой веткой.

        Без этого теста широкий except остаётся непроверенным: все остальные
        случаи уборки перехватываются веткой VkError.
        """

        def connection_lost(recorder, body):
            raise httpx.ConnectError("связь пропала")

        recorder = Recorder({"photos.delete": connection_lost})

        result = publisher(recorder, monkeypatch).publish(Post(), images)

        assert result == f"-{GROUP}_42", "публикация откатилась из-за сбоя уборки"

    def test_permanently_impossible_cleanup_is_attempted_only_once(
        self, images, monkeypatch
    ):
        """На живом API photos.delete запрещён не-standalone приложению навсегда.

        Без гашения на каждый пост уходило бы по два обречённых вызова и по два
        предупреждения в лог — тот самый шум, из-за которого перестают читать
        предупреждения вообще.
        """
        recorder = Recorder({"photos.delete": error(15, "non-standalone")})
        publisher_instance = publisher(recorder, monkeypatch)

        publisher_instance.publish(Post(), images)
        publisher_instance.publish(Post(), images)

        assert recorder.methods().count("photos.delete") == 1

    def test_a_temporary_cleanup_failure_does_not_switch_it_off(
        self, images, monkeypatch
    ):
        """Сетевой сбой — не повод бросать уборку навсегда."""
        recorder = Recorder({"photos.delete": error(6, "Too many requests")})
        publisher_instance = publisher(recorder, monkeypatch)

        publisher_instance.publish(Post(), images)
        publisher_instance.publish(Post(), images)

        assert recorder.methods().count("photos.delete") == 4

    def test_error_15_on_delete_explains_the_real_cause(self, images, monkeypatch):
        """«Проверь группу» — неверный совет: дело в типе приложения."""
        from factory.providers.publishers.vk import _advice

        assert "недоступен приложениям" in _advice(15, "photos.delete")


class TestErrorMessages:
    def test_expired_key_tells_how_to_refresh_it(self, images, monkeypatch):
        recorder = Recorder({"photos.getWallUploadServer": error(5, "auth failed")})

        with pytest.raises(ProviderError) as excinfo:
            publisher(recorder, monkeypatch).publish(Post(), images)

        message = str(excinfo.value)
        assert "24 часа" in message
        assert "RUNBOOK" in message

    def test_error_27_explains_that_the_keys_are_swapped(self, images, monkeypatch):
        recorder = Recorder({"photos.getWallUploadServer": error(27, "group auth")})

        with pytest.raises(ProviderError) as excinfo:
            publisher(recorder, monkeypatch).publish(Post(), images)

        message = str(excinfo.value)
        assert "перепутаны" in message
        assert "ВК-как-это-работает" in message

    def test_error_214_mentions_the_posting_limit(self, images, monkeypatch):
        recorder = Recorder({"wall.post": error(214, "Access to adding post denied")})

        with pytest.raises(ProviderError) as excinfo:
            publisher(recorder, monkeypatch).publish(Post(), images)

        assert "лимит" in str(excinfo.value)

    def test_missing_image_file_is_explained(self, tmp_path, monkeypatch):
        recorder = Recorder()
        missing = Asset("cover", 0, tmp_path / "нет-такого.png")

        with pytest.raises(ProviderError) as excinfo:
            publisher(recorder, monkeypatch).publish(Post(), [missing])

        assert "factory post retry" in str(excinfo.value)


class TestExpiredTokenIsRecognised:
    """Ключей два, и «ошибка 5» обязана сказать, какой именно менять.

    Картинки грузит ключ пользователя, публикует ключ сообщества. Протухает
    почти всегда первый, но без имени переменной владельцу это не помогает:
    он полезет менять не тот.
    """

    def test_the_upload_key_is_named(self, images, monkeypatch):
        recorder = Recorder({"photos.getWallUploadServer": error(5, "expired")})
        vk = publisher(
            recorder, monkeypatch,
            token_env="VK_TOKEN_GROUP", upload_token_env="VK_UPLOAD_TOKEN",
        )

        with pytest.raises(VkError) as excinfo:
            vk.publish(Post(), images)

        assert excinfo.value.token_env == "VK_UPLOAD_TOKEN"
        assert "VK_UPLOAD_TOKEN" in str(excinfo.value)

    def test_it_is_marked_as_an_expired_key(self, images, monkeypatch):
        """По этой отметке машина решает, звать ли владельца."""
        recorder = Recorder({"photos.getWallUploadServer": error(5, "expired")})
        vk = publisher(recorder, monkeypatch, upload_token_env="VK_UPLOAD_TOKEN")

        with pytest.raises(VkError) as excinfo:
            vk.publish(Post(), images)

        assert excinfo.value.token_expired is True

    def test_other_failures_are_not_expired_keys(self, images, monkeypatch):
        """Иначе владельца будили бы ключом по любому отказу ВК."""
        recorder = Recorder({"photos.getWallUploadServer": error(27, "group auth")})
        vk = publisher(recorder, monkeypatch, upload_token_env="VK_UPLOAD_TOKEN")

        with pytest.raises(VkError) as excinfo:
            vk.publish(Post(), images)

        assert excinfo.value.token_expired is False
