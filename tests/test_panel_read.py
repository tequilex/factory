"""Чтение данных в панели.

Задача 2 этапа: только выдача, ничего не меняется. Проверяется три вещи —
что без входа не отдаётся ничего, что цифры считаются теми же функциями, что
исполняют правила, и что отдача файлов не выпускает наружу лишнего.
"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from factory.core import db, paths
from factory.core.models import State
from factory.panel import auth
from factory.panel.app import create_app

PASSWORD = "пароль-для-чтения"

READ_ENDPOINTS = (
    "/api/overview",
    "/api/posts",
    "/api/posts/1",
    "/api/posts/1/image/0",
    "/api/states",
    "/api/topics/demo",
    "/api/spending",
    "/api/events",
)


@pytest.fixture
def panel(pipeline, monkeypatch):
    """Панель поверх готового проекта demo с одним постом."""
    monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
    monkeypatch.delenv(auth.SECRET_ENV, raising=False)
    auth.set_password(PASSWORD)
    client = TestClient(create_app())
    client.post("/api/login", json={"password": PASSWORD})
    return {"client": client, **pipeline}


@pytest.fixture
def closed(pipeline, monkeypatch):
    """Та же панель, но без входа."""
    monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
    monkeypatch.delenv(auth.SECRET_ENV, raising=False)
    auth.set_password(PASSWORD)
    return TestClient(create_app())


class TestNothingWithoutLogin:
    @pytest.mark.parametrize("path", READ_ENDPOINTS)
    def test_every_read_endpoint_is_closed(self, closed, path):
        """Забытая зависимость на одной ручке — открытые данные наружу.

        Проверяется списком именно поэтому: новая ручка, не попавшая под
        общий вход, обнаружится здесь, а не тем, что её кто-то прочитал.
        """
        assert closed.get(path).status_code == 401


class TestOverview:
    def test_it_shows_the_group(self, panel):
        body = panel["client"].get("/api/overview").json()

        assert [group["slug"] for group in body["groups"]] == ["demo"]

    def test_waiting_is_counted(self, panel):
        # Шаг сам состояние не двигает — это делает стейт-машина. В тесте пост
        # ставится в нужное состояние напрямую, как это делает фикстура.
        panel["context"](State.IN_REVIEW)

        group = panel["client"].get("/api/overview").json()["groups"][0]

        assert group["waiting"] == 1
        assert group["working"] == 0

    def test_a_post_is_counted_once(self, panel):
        """Пост, попавший в две колонки сразу, ломает сводку на экране."""
        panel["advance_through"](State.QUEUED)

        group = panel["client"].get("/api/overview").json()["groups"][0]

        assert group["waiting"] + group["approved"] + group["working"] + group["failed"] == 1

    def test_health_reports_a_silent_worker(self, panel):
        """Воркер, который не отработал ни разу, так же сломан, как вставший."""
        body = panel["client"].get("/api/overview").json()

        assert body["health"]["stale"] is True
        assert body["health"]["tick_age_sec"] is None

    def test_limits_come_from_the_config(self, panel):
        group = panel["client"].get("/api/overview").json()["groups"][0]

        assert group["posts_per_day"] == panel["project"].limits.posts_per_day

    def test_spending_is_summed(self, panel):
        from factory.core.retry import record_run

        record_run(
            panel["conn"], step="queued", ok=True, duration_ms=1,
            post_id=panel["post_id"], cost_usd=0.25,
        )

        group = panel["client"].get("/api/overview").json()["groups"][0]

        assert group["spent_month"] == pytest.approx(0.25)

    def test_a_broken_project_is_not_hidden(self, panel, demo_project):
        """Пропавшая с экрана группа выглядит как удалённая. Так нельзя."""
        (demo_project / "config.yaml").write_text("slug: demo\nвсё: сломано\n", encoding="utf-8")

        body = panel["client"].get("/api/overview").json()

        assert "demo" in body["broken"]
        assert body["groups"] == []


class TestPost:
    def test_states_are_the_real_codes(self, panel):
        """Выдуманные коды в макете были; в ответе их быть не должно."""
        body = panel["client"].get("/api/states").json()

        assert set(body) == {state.value for state in State}
        assert body["in_review"] == "ждёт вашего решения"

    def test_detail_carries_text_and_label(self, panel):
        panel["advance_through"](State.QUEUED)
        panel["context"](State.TEXT_READY)

        body = panel["client"].get(f"/api/posts/{panel['post_id']}").json()

        assert body["state"] == State.TEXT_READY
        assert body["state_label"] == "проверяются факты"
        assert body["body"], "шаг написал текст, а панель его не показала"

    def test_a_fresh_post_is_variant_one_of_one(self, panel):
        """В post_versions пусто до первой отправки на ревью — «1 из 0» это ошибка."""
        body = panel["client"].get(f"/api/posts/{panel['post_id']}").json()

        assert body["version"] == 1
        assert body["versions_total"] == 1

    def test_assets_are_listed_cover_first(self, panel):
        panel["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )

        assets = panel["client"].get(f"/api/posts/{panel['post_id']}").json()["assets"]

        assert assets[0]["kind"] == "cover"
        assert all(item["ready"] for item in assets)
        assert assets[0]["prompt"]

    def test_a_missing_post_is_a_clear_404(self, panel):
        response = panel["client"].get("/api/posts/99999")

        assert response.status_code == 404
        assert "нет" in response.json()["detail"]

    def test_the_list_filters_by_state(self, panel):
        client = panel["client"]

        assert client.get("/api/posts", params={"state": State.PUBLISHED}).json() == []
        assert len(client.get("/api/posts", params={"state": State.QUEUED}).json()) == 1


class TestImages:
    def _generate(self, panel):
        panel["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )

    def test_a_generated_image_is_served(self, panel):
        self._generate(panel)

        response = panel["client"].get(f"/api/posts/{panel['post_id']}/image/0")

        assert response.status_code == 200
        assert Image.open(io.BytesIO(response.content)).size == (1080, 1350)

    def test_a_vanished_file_is_not_reported_as_ready(self, panel):
        """Запись в базе и файл на диске — разные вещи.

        После публикации папка поста вычищается, а строки в assets остаются:
        в них история промптов и seed'ов. Панель, верящая базе, показывала
        битые картинки — браузер пытался их загрузить и не мог.
        """
        self._generate(panel)
        from pathlib import Path

        row = panel["conn"].execute(
            "SELECT local_path FROM assets WHERE post_id = ? AND position = 0",
            (panel["post_id"],),
        ).fetchone()
        Path(row["local_path"]).unlink()

        body = panel["client"].get(f"/api/posts/{panel['post_id']}").json()
        brief = panel["client"].get("/api/posts").json()[0]

        assert body["assets"][0]["ready"] is False
        assert brief["has_cover"] is False

    def test_an_absent_image_is_a_404(self, panel):
        response = panel["client"].get(f"/api/posts/{panel['post_id']}/image/0")

        assert response.status_code == 404

    def test_a_path_outside_the_store_is_refused(self, panel):
        """Панель читает файлы правами воркера — значит и файл секретов тоже.

        Путь берётся из базы, но одна испорченная строка не должна открывать
        наружу всё, до чего дотягивается процесс.
        """
        self._generate(panel)
        outsider = paths.env_file()
        outsider.parent.mkdir(parents=True, exist_ok=True)
        outsider.write_text("VK_TOKEN_GROUP=секрет\n", encoding="utf-8")
        with db.write_transaction(panel["conn"]):
            panel["conn"].execute(
                "UPDATE assets SET local_path = ? WHERE post_id = ? AND position = 0",
                (str(outsider), panel["post_id"]),
            )

        response = panel["client"].get(f"/api/posts/{panel['post_id']}/image/0")

        assert response.status_code == 404
        assert "секрет" not in response.text


class TestTopicsAndMoney:
    def test_topics_are_counted_and_listed(self, panel):
        body = panel["client"].get("/api/topics/demo").json()

        assert body["free"] + body["taken"] + body["used"] == 1
        assert body["days_left"] is not None

    def test_an_unknown_project_is_a_clear_404(self, panel):
        response = panel["client"].get("/api/topics/нет-такого")

        assert response.status_code == 404
        assert "не подключён" in response.json()["detail"]

    def test_spending_splits_by_step(self, panel):
        """Разбивка нужна, чтобы было видно: картинки — почти вся цена поста."""
        from factory.core.retry import record_run

        for step, cost in (("queued", 0.18), ("text_ready", 0.10), ("prompts_ready", 6.72)):
            record_run(
                panel["conn"], step=step, ok=True, duration_ms=1,
                post_id=panel["post_id"], cost_usd=cost,
            )

        body = panel["client"].get("/api/spending").json()

        day = body["days"][0]
        assert day["text"] == pytest.approx(0.18)
        assert day["factcheck"] == pytest.approx(0.10)
        assert day["images"] == pytest.approx(6.72)
        assert body["average_post"] == pytest.approx(7.0)

    def test_average_is_per_post_not_a_total(self, panel):
        """С одним постом среднее совпадает с суммой — и проверка ничего не значит.

        Нужен второй пост, иначе «средняя цена» может оказаться просто итогом,
        и на экране владелец увидит удвоенную цену поста.
        """
        from factory.core.retry import record_run
        from tests.conftest import insert_post, insert_topic

        second = insert_post(
            panel["conn"],
            panel["project_id"],
            insert_topic(panel["conn"], panel["project_id"], "Вторая"),
            idem_key="demo:second:0",
        )
        panel["conn"].commit()
        record_run(
            panel["conn"], step="queued", ok=True, duration_ms=1,
            post_id=panel["post_id"], cost_usd=4.00,
        )
        record_run(
            panel["conn"], step="queued", ok=True, duration_ms=1,
            post_id=second, cost_usd=2.00,
        )

        body = panel["client"].get("/api/spending").json()

        assert body["total"] == pytest.approx(6.0)
        assert body["posts"] == 2
        assert body["average_post"] == pytest.approx(3.0)

    def test_events_speak_human(self, panel):
        panel["advance_through"](State.QUEUED)

        body = panel["client"].get("/api/events").json()

        assert body[0]["step_label"] == "написан текст"
        assert body[0]["ok"] is True

    def test_only_errors_filters(self, panel):
        from factory.core.retry import record_run

        record_run(panel["conn"], step="queued", ok=True, duration_ms=1, post_id=panel["post_id"])
        record_run(
            panel["conn"], step="prompts_ready", ok=False, duration_ms=1,
            post_id=panel["post_id"], error="не нарисовалось",
        )

        body = panel["client"].get("/api/events", params={"only_errors": True}).json()

        assert [item["error"] for item in body] == ["не нарисовалось"]
