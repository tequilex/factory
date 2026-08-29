"""Действия над постом из панели.

Панель — второй интерфейс к той же логике, что у бота. Проверяется поэтому не
«запрос вернул 200», а что решение применилось теми же правилами: сторожа сняты,
устаревшее нажатие отвергнуто с объяснением, вариант подменяется только у поста,
который правда ждёт решения.
"""

import pytest
from fastapi.testclient import TestClient

from factory.core import db, versions
from factory.core.decisions import Decision
from factory.core.models import Post, State
from factory.panel import auth
from factory.panel.app import create_app

PASSWORD = "пароль-для-действий"


@pytest.fixture
def panel(pipeline, monkeypatch):
    """Пост, доведённый до ревью, и вошедшая панель."""
    monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
    monkeypatch.delenv(auth.SECRET_ENV, raising=False)
    auth.set_password(PASSWORD)
    pipeline["advance_through"](
        State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
        State.PROMPTS_READY, State.IMAGES_READY, State.COMPOSED,
    )
    pipeline["context"](State.IN_REVIEW)

    client = TestClient(create_app())
    client.post("/api/login", json={"password": PASSWORD})

    def state() -> str:
        return pipeline["conn"].execute(
            "SELECT state FROM posts WHERE id = ?", (pipeline["post_id"],)
        ).fetchone()["state"]

    def decide(decision: str, **extra):
        return client.post(
            f"/api/posts/{pipeline['post_id']}/decision",
            json={"decision": decision, **extra},
        )

    return {"client": client, "state": state, "decide": decide, **pipeline}


class TestDecisions:
    def test_approve_moves_the_post(self, panel):
        response = panel["decide"](Decision.APPROVE)

        assert response.status_code == 200
        assert panel["state"]() == State.APPROVED
        assert response.json()["state"] == State.APPROVED

    def test_a_silent_worker_is_named_in_the_answer(self, panel):
        """Панель не имеет права обещать работу, которую некому выполнить.

        Поймано на живом экране: владелец отправил картинку на перерисовку и
        поставил свою, получил бодрые подтверждения и не дождался ничего. Обе
        команды записались верно — работать было некому.
        """
        body = panel["decide"](Decision.APPROVE).json()

        assert "воркер" in body["what_next"].lower()
        assert "не потеряно" in body["what_next"]

    def test_a_working_worker_adds_no_warning(self, panel):
        """Приписка про молчание не должна висеть всегда — иначе её перестанут читать."""
        from factory.core import lock

        lock.write_heartbeat(panel["conn"])

        body = panel["decide"](Decision.APPROVE).json()

        assert "воркер" not in body["what_next"].lower()

    def test_the_answer_says_what_happens_next(self, panel):
        """«Готово» здесь всегда враньё: выполняет воркер, а не панель."""
        body = panel["decide"](Decision.APPROVE).json()

        assert "уйдёт в группу" in body["what_next"].lower()

    def test_text_again_clears_the_factcheck(self, panel):
        """Сторожа снимает decisions.apply(), и панель обязана ходить через него.

        Забытый вердикт означал бы новый текст со старой проверкой — ошибка,
        ради которой список сторожей и заведён.
        """
        with db.write_transaction(panel["conn"]):
            panel["conn"].execute(
                "UPDATE posts SET factcheck_verdict = 'ok' WHERE id = ?", (panel["post_id"],)
            )

        panel["decide"](Decision.TEXT)

        row = panel["conn"].execute(
            "SELECT body, factcheck_verdict FROM posts WHERE id = ?", (panel["post_id"],)
        ).fetchone()
        assert row["factcheck_verdict"] is None
        assert row["body"] is None

    def test_a_stale_press_is_explained_not_broken(self, panel):
        """Экран мог устареть: пост уехал дальше с другого устройства."""
        panel["decide"](Decision.APPROVE)

        response = panel["decide"](Decision.APPROVE)

        assert response.status_code == 409
        assert "уже одобрен" in response.json()["detail"]

    def test_cancel_returns_an_approved_post(self, panel):
        panel["decide"](Decision.APPROVE)

        panel["decide"](Decision.CANCEL)

        assert panel["state"]() == State.IN_REVIEW

    def test_a_published_post_cannot_be_cancelled(self, panel):
        """Пост уже видят подписчики — отметка в базе их не уберёт."""
        panel["decide"](Decision.APPROVE)
        with db.write_transaction(panel["conn"]):
            panel["conn"].execute(
                "UPDATE posts SET state = ?, external_id = 'vk_1' WHERE id = ?",
                (State.PUBLISHED, panel["post_id"]),
            )

        response = panel["decide"](Decision.CANCEL)

        assert response.status_code == 409
        assert "в самой группе" in response.json()["detail"]

    def test_a_missing_post_is_a_404(self, panel):
        response = panel["client"].post(
            "/api/posts/99999/decision", json={"decision": Decision.APPROVE}
        )

        assert response.status_code == 404

    def test_an_unknown_decision_is_refused(self, panel):
        """Список решений закрыт: выдуманное значение не должно доехать до базы."""
        response = panel["decide"]("удали-всё")

        assert response.status_code == 422
        assert panel["state"]() == State.IN_REVIEW


class TestEdits:
    def test_text_only_keeps_the_cover(self, panel):
        response = panel["client"].post(
            f"/api/posts/{panel['post_id']}/text", json={"body": "Новый текст поста."}
        )

        assert response.status_code == 200
        assert panel["state"]() == State.COMPOSED
        assert "денег это не стоит" in response.json()["what_next"]

    def test_a_new_title_rebuilds_the_cover(self, panel):
        """Заголовок напечатан на обложке — её надо собрать заново."""
        response = panel["client"].post(
            f"/api/posts/{panel['post_id']}/text",
            json={"title": "Совсем другой заголовок", "body": "Текст."},
        )

        assert panel["state"]() == State.IMAGES_READY
        assert "обложка соберётся заново" in response.json()["what_next"]

    def test_an_empty_text_is_refused(self, panel):
        response = panel["client"].post(
            f"/api/posts/{panel['post_id']}/text", json={"body": ""}
        )

        assert response.status_code == 422

    def test_editing_a_post_that_moved_on_is_explained(self, panel):
        panel["decide"](Decision.APPROVE)

        response = panel["client"].post(
            f"/api/posts/{panel['post_id']}/text", json={"body": "Поздно."}
        )

        assert response.status_code == 409


class TestVersions:
    def _save_variant(self, panel) -> int:
        # record() открывает транзакцию сам — своя снаружи дала бы вложенную.
        row = panel["conn"].execute(
            "SELECT * FROM posts WHERE id = ?", (panel["post_id"],)
        ).fetchone()
        return versions.record(panel["conn"], Post.from_row(row))

    def test_a_saved_variant_can_be_shown(self, panel):
        number = self._save_variant(panel)
        with db.write_transaction(panel["conn"]):
            panel["conn"].execute(
                "UPDATE posts SET title = 'другой' WHERE id = ?", (panel["post_id"],)
            )

        response = panel["client"].post(f"/api/posts/{panel['post_id']}/version/{number}")

        assert response.status_code == 200
        title = panel["conn"].execute(
            "SELECT title FROM posts WHERE id = ?", (panel["post_id"],)
        ).fetchone()["title"]
        assert title != "другой"

    def test_approving_publishes_the_variant_you_pressed_on(self, panel):
        """Одобряется тот вариант, под которым нажали, а не последний сделанный.

        Без этого «Опубликовать» на старом варианте уходило содержимым нового —
        и владелец узнавал об этом уже по посту в группе.
        """
        first = self._save_variant(panel)
        with db.write_transaction(panel["conn"]):
            panel["conn"].execute(
                "UPDATE posts SET title = 'второй вариант', version = 2 WHERE id = ?",
                (panel["post_id"],),
            )
        self._save_variant(panel)

        panel["decide"](Decision.APPROVE, version=first)

        row = panel["conn"].execute(
            "SELECT title, version FROM posts WHERE id = ?", (panel["post_id"],)
        ).fetchone()
        assert row["version"] == first
        assert row["title"] != "второй вариант"

    def test_a_missing_variant_is_refused(self, panel):
        response = panel["client"].post(f"/api/posts/{panel['post_id']}/version/99")

        assert response.status_code == 409

    def test_a_post_that_moved_on_keeps_its_variant(self, panel):
        """Ровно та дыра, ради которой варианты и заводились.

        Пост уехал на переделку, а вариант в базе подменился — и картинки нового
        варианта легли бы поверх файлов старого. Оба были бы потеряны.
        """
        number = self._save_variant(panel)
        panel["decide"](Decision.APPROVE)
        before = panel["conn"].execute(
            "SELECT version FROM posts WHERE id = ?", (panel["post_id"],)
        ).fetchone()["version"]

        response = panel["client"].post(f"/api/posts/{panel['post_id']}/version/{number}")

        assert response.status_code == 409
        after = panel["conn"].execute(
            "SELECT version FROM posts WHERE id = ?", (panel["post_id"],)
        ).fetchone()["version"]
        assert after == before


class TestClosedWithoutLogin:
    @pytest.mark.parametrize(
        "path", ["/api/posts/1/decision", "/api/posts/1/text", "/api/posts/1/version/1"]
    )
    def test_actions_need_a_login(self, pipeline, monkeypatch, path):
        monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
        monkeypatch.delenv(auth.SECRET_ENV, raising=False)
        auth.set_password(PASSWORD)
        client = TestClient(create_app())

        assert client.post(path, json={"decision": "ok", "body": "текст"}).status_code == 401
