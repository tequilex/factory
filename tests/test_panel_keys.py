"""Экран ключей и добавление тем из панели.

Ключи — самая частая поломка системы, а темы кончаются тише всего. Проверяется
здесь то, что делает эти экраны честными: ключ не показывается целиком, живость
проверяется вызовом, а не сроком, и добавление тем снимает тревогу о том, что
публиковать нечего.
"""

import pytest
from fastapi.testclient import TestClient

from factory.core import alerts, topics
from factory.panel import auth
from factory.panel.app import create_app

PASSWORD = "пароль-для-ключей"


@pytest.fixture
def panel(pipeline, monkeypatch):
    monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
    monkeypatch.delenv(auth.SECRET_ENV, raising=False)
    auth.set_password(PASSWORD)
    monkeypatch.setenv("VK_TOKEN_DEMO", "vk1.a." + "x" * 40)
    client = TestClient(create_app())
    client.post("/api/login", json={"password": PASSWORD})
    return {"client": client, **pipeline}


class TestKeys:
    def test_keys_are_listed_with_only_their_tails(self, panel):
        """Панель открыта в браузере, а браузеры хранят историю и кэш."""
        body = panel["client"].get("/api/groups/demo/keys").json()

        community = next(key for key in body["keys"] if key["title"] == "Ключ сообщества")
        assert community["present"] is True
        assert community["tail"] == "…xxxx"
        assert "x" * 40 not in str(body)

    def test_a_missing_key_is_shown_as_missing(self, panel):
        body = panel["client"].get("/api/groups/demo/keys").json()

        upload = body["keys"][0]
        assert upload["present"] is False
        assert upload["alive"] is None

    def test_each_key_reports_its_own_presence(self, panel, monkeypatch):
        """Зелёная галочка у ключа, которого нет, — худший вид вранья на экране.

        Владелец увидит «всё задано» и пойдёт искать поломку куда угодно, кроме
        того места, где она есть.
        """
        monkeypatch.delenv("VK_TOKEN_DEMO", raising=False)

        body = panel["client"].get("/api/groups/demo/keys").json()

        community = next(key for key in body["keys"] if key["title"] == "Ключ сообщества")
        assert community["present"] is False
        assert community["tail"] is None

    def test_an_unknown_group_is_a_404(self, panel):
        assert panel["client"].get("/api/groups/нет/keys").status_code == 404

    def test_a_code_without_app_settings_is_refused_clearly(self, panel):
        """У demo нет app_id — обменивать код не на что, и надо сказать почему."""
        response = panel["client"].post(
            "/api/groups/demo/vk-code",
            json={"text": "https://oauth.vk.ru/blank.html#code=4b59a4fb40ab1805e3"},
        )

        assert response.status_code == 409
        assert "vk.app_id" in response.json()["detail"]

    def test_text_without_a_code_is_explained(self, panel, monkeypatch):
        """Владелец присылает адрес с телефона: обрезанный код — обычное дело."""
        from factory.core.config import load_project

        project = load_project("demo")
        patched = project.model_copy(
            update={"vk": project.vk.model_copy(update={"app_id": 1, "app_secret_env": "VK_APP_SECRET"})}
        )
        monkeypatch.setattr("factory.panel.deps.projects", lambda: {"demo": patched})

        response = panel["client"].post("/api/groups/demo/vk-code", json={"text": "просто текст"})

        assert response.status_code == 422
        assert "нет кода" in response.json()["detail"]


class TestAddTopics:
    def test_topics_are_added(self, panel):
        response = panel["client"].post(
            "/api/topics/demo", json={"text": "Первая тема\nВторая тема"}
        )

        assert response.status_code == 200
        assert response.json()["added"] == 2
        assert topics.counts(panel["conn"], panel["project_id"]).free >= 2

    def test_repeats_are_counted_not_added(self, panel):
        panel["client"].post("/api/topics/demo", json={"text": "Одна и та же"})

        body = panel["client"].post("/api/topics/demo", json={"text": "Одна и та же"}).json()

        assert body["added"] == 0
        assert body["skipped"] == 1
        # Сообщение обязано совпадать с числами: владелец читает его, а не поля.
        assert "Добавлено тем: 0" in body["what_next"]
        assert "Повторов пропущено: 1" in body["what_next"]

    def test_a_silent_worker_is_named_here_too(self, panel):
        """Тема добавлена — но пост из неё делает воркер.

        Поймано живьём: владелец добавил тему, увидел «Добавлено тем: 1» и не
        дождался поста. Предупреждение стояло только на действиях с постами.
        """
        body = panel["client"].post("/api/topics/demo", json={"text": "Свежая тема"}).json()

        assert "воркер" in body["what_next"].lower()
        assert "не потеряно" in body["what_next"]

    def test_nothing_added_means_nothing_promised(self, panel):
        """Одни повторы — обещать пост не за что."""
        panel["client"].post("/api/topics/demo", json={"text": "Повтор"})

        body = panel["client"].post("/api/topics/demo", json={"text": "Повтор"}).json()

        assert "воркер" not in body["what_next"].lower()

    def test_empty_lines_are_not_topics(self, panel):
        response = panel["client"].post("/api/topics/demo", json={"text": "   \n\n  "})

        assert response.status_code == 422

    def test_adding_clears_the_alarm_about_running_out(self, panel):
        """Тревога снимается там, где видно, что причина исчезла.

        Иначе «скоро публиковать нечего» висит после того, как темы уже
        засыпаны, и следующая такая же тревога не прозвучит.
        """
        alerts.raise_once(
            panel["conn"], panel["providers"].notifier, chat_id=1,
            name="no_topics", scope="demo", text="кончаются",
        )
        assert alerts.is_raised(panel["conn"], "no_topics", "demo")

        panel["client"].post("/api/topics/demo", json={"text": "Новая тема"})

        assert not alerts.is_raised(panel["conn"], "no_topics", "demo")

    def test_an_unknown_project_is_a_404(self, panel):
        assert panel["client"].post("/api/topics/нет", json={"text": "Тема"}).status_code == 404


class TestClosedWithoutLogin:
    @pytest.mark.parametrize("path", ["/api/groups/demo/keys"])
    def test_reading_keys_needs_a_login(self, pipeline, monkeypatch, path):
        monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
        monkeypatch.delenv(auth.SECRET_ENV, raising=False)
        auth.set_password(PASSWORD)

        assert TestClient(create_app()).get(path).status_code == 401

    def test_adding_topics_needs_a_login(self, pipeline, monkeypatch):
        monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
        monkeypatch.delenv(auth.SECRET_ENV, raising=False)
        auth.set_password(PASSWORD)

        response = TestClient(create_app()).post("/api/topics/demo", json={"text": "Тема"})

        assert response.status_code == 401
