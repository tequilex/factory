"""Настройки группы через панель."""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from factory.core.config import load_project
from factory.panel import auth
from factory.panel.app import create_app

PASSWORD = "пароль-для-настроек"


@pytest.fixture
def panel(pipeline, monkeypatch):
    monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
    monkeypatch.delenv(auth.SECRET_ENV, raising=False)
    auth.set_password(PASSWORD)
    client = TestClient(create_app())
    client.post("/api/login", json={"password": PASSWORD})
    return {"client": client, **pipeline}


class TestReading:
    def test_settings_are_shown_by_section(self, panel):
        body = panel["client"].get("/api/groups/demo/settings").json()

        assert body["slug"] == "demo"
        assert body["values"]["limits"]["posts_per_day"] == 2
        assert "# " in body["raw"], "файл отдаётся как есть, вместе с пояснениями"

    def test_an_unknown_group_is_a_404(self, panel):
        assert panel["client"].get("/api/groups/нет/settings").status_code == 404


class TestSaving:
    def test_a_change_reaches_the_config(self, panel):
        response = panel["client"].post(
            "/api/groups/demo/settings",
            json={"changes": {"limits": {"posts_per_day": 4, "queue_buffer": 12}}},
        )

        assert response.status_code == 200
        assert load_project("demo").limits.posts_per_day == 4

    def test_the_answer_does_not_promise_a_restart(self, panel):
        """Воркер перечитывает конфиг каждый проход — просить перезапуск незачем."""
        body = panel["client"].post(
            "/api/groups/demo/settings",
            json={"changes": {"limits": {"posts_per_day": 4, "queue_buffer": 12}}},
        ).json()

        assert "перезапускать ничего не нужно" in body["what_next"]

    def test_a_bad_value_is_refused_with_a_human_reason(self, panel):
        response = panel["client"].post(
            "/api/groups/demo/settings",
            json={"changes": {"limits": {"posts_per_day": 0}}},
        )

        assert response.status_code == 422
        assert "posts_per_day" in response.json()["detail"]
        assert load_project("demo").limits.posts_per_day == 2

    def test_a_section_outside_the_list_is_refused(self, panel):
        """Список разделов закрыт: новая настройка не появляется в панели сама.

        Появившись сама, она пришла бы без подписи и объяснения — а владелец
        не читает код и понять её сможет только по названию поля.
        """
        response = panel["client"].post(
            "/api/groups/demo/settings", json={"changes": {"root": "/чужой/путь"}}
        )

        assert response.status_code == 422
        assert "нельзя менять" in response.json()["detail"]

    def test_preview_shows_the_file_without_writing_it(self, panel):
        before = load_project("demo").limits.posts_per_day

        body = panel["client"].post(
            "/api/groups/demo/settings/preview",
            json={"changes": {"limits": {"posts_per_day": 5, "queue_buffer": 15}}},
        ).json()

        assert "posts_per_day: 5" in body["raw"]
        assert load_project("demo").limits.posts_per_day == before

    def test_preview_refuses_the_same_things_saving_does(self, panel):
        response = panel["client"].post(
            "/api/groups/demo/settings/preview",
            json={"changes": {"limits": {"posts_per_day": -1}}},
        )

        assert response.status_code == 422


class TestProjectFiles:
    def test_a_prompt_is_saved(self, panel):
        response = panel["client"].post(
            "/api/groups/demo/file",
            json={"path": "prompts/voice.md", "text": "Ты — новый персонаж."},
        )

        assert response.status_code == 200
        assert load_project("demo").voice() == "Ты — новый персонаж."

    def test_a_path_outside_the_project_is_refused(self, panel):
        response = panel["client"].post(
            "/api/groups/demo/file",
            json={"path": "../../data/.env", "text": "VK_TOKEN_GROUP=чужой"},
        )

        assert response.status_code == 422

    def test_the_reference_needs_to_be_configured_first(self, panel):
        """У demo эталонного портрета нет — предлагать замену нечему."""
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64)).save(buffer, format="PNG")

        response = panel["client"].post(
            "/api/groups/demo/reference",
            files={"file": ("canon.png", buffer.getvalue(), "image/png")},
        )

        assert response.status_code == 409
        assert "image.reference" in response.json()["detail"]


class TestClosedWithoutLogin:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/groups/demo/settings"),
            ("post", "/api/groups/demo/settings"),
            ("post", "/api/groups/demo/settings/preview"),
            ("post", "/api/groups/demo/file"),
        ],
    )
    def test_settings_need_a_login(self, pipeline, monkeypatch, method, path):
        monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
        monkeypatch.delenv(auth.SECRET_ENV, raising=False)
        auth.set_password(PASSWORD)
        client = TestClient(create_app())

        payload = {"changes": {"limits": {}}, "path": "x", "text": "y"}
        response = (
            client.get(path) if method == "get" else client.post(path, json=payload)
        )

        assert response.status_code == 401
