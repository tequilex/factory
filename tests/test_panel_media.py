"""То, чего в системе не было до панели.

Подмена картинки своей, перерисовка одной по промпту, порядок тем. Всё три —
откаты, и проверяется у них главное: сняты ли отметки, на которые смотрят шаги.
Забытая отметка означает, что откат не случится вовсе, а выглядеть это будет
как «нажал, и ничего».
"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from factory.core import db, paths, topics
from factory.core.models import State
from factory.panel import auth
from factory.panel.app import create_app
from tests.conftest import insert_topic

PASSWORD = "пароль-для-картинок"


def png(width: int = 800, height: int = 800, colour=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def panel(pipeline, monkeypatch):
    monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
    monkeypatch.delenv(auth.SECRET_ENV, raising=False)
    auth.set_password(PASSWORD)
    pipeline["advance_through"](
        State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
        State.PROMPTS_READY, State.IMAGES_READY, State.COMPOSED,
    )
    # Пост дошёл до просмотра: альбом отправлен, обложка собрана.
    with db.write_transaction(pipeline["conn"]):
        pipeline["conn"].execute(
            "UPDATE posts SET state = ?, review_message_id = 5, review_album_at = '2026-01-01', "
            "review_album_message_id = 4 WHERE id = ?",
            (State.IN_REVIEW, pipeline["post_id"]),
        )

    client = TestClient(create_app())
    client.post("/api/login", json={"password": PASSWORD})

    def post_row():
        return pipeline["conn"].execute(
            "SELECT * FROM posts WHERE id = ?", (pipeline["post_id"],)
        ).fetchone()

    def asset(position: int):
        return pipeline["conn"].execute(
            "SELECT * FROM assets WHERE post_id = ? AND position = ?",
            (pipeline["post_id"], position),
        ).fetchone()

    return {"client": client, "post_row": post_row, "asset": asset, **pipeline}


class TestOwnImage:
    def _upload(self, panel, position: int = 1, data: bytes | None = None):
        return panel["client"].post(
            f"/api/posts/{panel['post_id']}/image/{position}",
            files={"file": ("своя.png", data if data is not None else png(), "image/png")},
        )

    def test_the_file_replaces_the_generated_one(self, panel):
        response = self._upload(panel)

        assert response.status_code == 200
        assert panel["asset"](1)["replaced_by_owner"] == 1

    def test_it_is_brought_to_the_post_size(self, panel):
        """Своя картинка приходит с телефона любого размера.

        Оставить как есть нельзя: ВК обрежет её по-своему, а обложка соберётся
        по чужому макету.
        """
        self._upload(panel, data=png(1600, 900))

        saved = Image.open(panel["asset"](1)["local_path"])
        assert saved.size == (1080, 1350)

    def test_the_album_marks_are_cleared(self, panel):
        """Картинки изменились — старый альбом показывать нельзя.

        Без снятия отметок пост вернётся на просмотр с прежними картинками и
        подписью «вот новые», то есть соврёт.
        """
        self._upload(panel)

        row = panel["post_row"]()
        assert row["review_album_at"] is None
        assert row["review_message_id"] is None
        assert row["state"] == State.COMPOSED

    def test_replacing_the_cover_asks_for_a_rebuild(self, panel):
        """На обложке печатается заголовок — её надо собрать заново."""
        self._upload(panel, position=0)

        assert panel["asset"](0)["external_ref"] is None
        assert panel["post_row"]()["state"] == State.IMAGES_READY

    def test_a_file_that_is_not_a_picture_is_explained(self, panel):
        response = self._upload(panel, data="это просто текст, а не картинка".encode("utf-8"))

        assert response.status_code == 422
        assert "не картинка" in response.json()["detail"].lower()

    def test_a_post_that_moved_on_is_not_touched(self, panel):
        with db.write_transaction(panel["conn"]):
            panel["conn"].execute(
                "UPDATE posts SET state = ? WHERE id = ?",
                (State.APPROVED, panel["post_id"]),
            )

        response = self._upload(panel)

        assert response.status_code == 409
        assert panel["asset"](1)["replaced_by_owner"] == 0


class TestRedraw:
    def _redraw(self, panel, position: int = 1, prompt: str | None = None):
        return panel["client"].post(
            f"/api/posts/{panel['post_id']}/redraw/{position}",
            json={"prompt": prompt},
        )

    def test_only_that_image_loses_its_file(self, panel):
        """Шаг рисует только то, у чего нет файла: остальные три бесплатны."""
        before = [panel["asset"](i)["local_path"] for i in (0, 2, 3)]

        self._redraw(panel)

        assert panel["asset"](1)["local_path"] is None
        assert [panel["asset"](i)["local_path"] for i in (0, 2, 3)] == before

    def test_the_seed_changes(self, panel):
        """Тот же seed вернул бы ту же картинку — перерисовка была бы пустой."""
        before = panel["asset"](1)["seed"]

        self._redraw(panel)

        assert panel["asset"](1)["seed"] != before

    def test_an_edited_prompt_is_saved(self, panel):
        self._redraw(panel, prompt="woman fixing a bicycle, evening light")

        assert panel["asset"](1)["prompt"] == "woman fixing a bicycle, evening light"

    def test_without_a_prompt_the_old_one_stays(self, panel):
        before = panel["asset"](1)["prompt"]

        self._redraw(panel)

        assert panel["asset"](1)["prompt"] == before

    def test_the_post_goes_back_to_drawing(self, panel):
        self._redraw(panel)

        assert panel["post_row"]()["state"] == State.PROMPTS_READY

    def test_a_post_that_moved_on_is_not_touched(self, panel):
        with db.write_transaction(panel["conn"]):
            panel["conn"].execute(
                "UPDATE posts SET state = ? WHERE id = ?",
                (State.REJECTED, panel["post_id"]),
            )
        before = panel["asset"](1)["local_path"]

        response = self._redraw(panel)

        assert response.status_code == 409
        assert panel["asset"](1)["local_path"] == before


class TestOrder:
    def _free_ids(self, panel) -> list[int]:
        return [
            row["id"]
            for row in panel["conn"].execute(
                "SELECT id FROM topics WHERE project_id = ? AND status = 'free' "
                f"ORDER BY {topics.QUEUE_ORDER}",
                (panel["project_id"],),
            ).fetchall()
        ]

    def test_the_queue_follows_the_given_order(self, panel):
        for n in range(3):
            insert_topic(panel["conn"], panel["project_id"], f"Тема {n}")
        panel["conn"].commit()
        # Порядок задаётся списком целиком, поэтому и перечислять надо все
        # свободные темы, включая ту, что пришла с фикстурой.
        wanted = list(reversed(self._free_ids(panel)))

        response = panel["client"].put("/api/topics/demo/order", json={"ids": wanted})

        assert response.status_code == 200
        assert self._free_ids(panel) == wanted

    def test_the_worker_takes_the_first_one(self, panel):
        """Порядок на экране обязан совпадать с тем, что возьмёт воркер.

        Раньше список показывался по номеру строки, а бралась тема с учётом
        возврата в конец — и владелец видел не то, что произойдёт.
        """
        from factory.core.machine import claim_free_topic

        for n in range(3):
            insert_topic(panel["conn"], panel["project_id"], f"Тема {n}")
        panel["conn"].commit()
        wanted = list(reversed(self._free_ids(panel)))
        panel["client"].put("/api/topics/demo/order", json={"ids": wanted})

        assert claim_free_topic(panel["conn"], panel["project_id"]) == wanted[0]

    def test_a_topic_already_taken_is_not_moved(self, panel):
        """Воркер мог забрать тему, пока владелец тащил её мышкой."""
        taken = insert_topic(panel["conn"], panel["project_id"], "Уже в работе")
        with db.write_transaction(panel["conn"]):
            panel["conn"].execute(
                "UPDATE topics SET status = 'taken' WHERE id = ?", (taken,)
            )

        panel["client"].put("/api/topics/demo/order", json={"ids": [taken]})

        position = panel["conn"].execute(
            "SELECT position FROM topics WHERE id = ?", (taken,)
        ).fetchone()["position"]
        assert position == 0

    def test_topics_of_another_group_are_not_touched(self, panel):
        """Групп несколько, и номера тем сквозные.

        Без проверки проекта перестановка в одной группе меняла бы порядок
        очереди в другой — а заметить это можно было бы только по тому, что
        соседняя ниша начала писать не про то.
        """
        from tests.conftest import insert_project

        other = insert_project(panel["conn"], "чужая")
        alien = insert_topic(panel["conn"], other, "Чужая тема")
        panel["conn"].commit()

        panel["client"].put("/api/topics/demo/order", json={"ids": [alien]})

        position = panel["conn"].execute(
            "SELECT position FROM topics WHERE id = ?", (alien,)
        ).fetchone()["position"]
        assert position == 0

    def test_an_unknown_project_is_a_404(self, panel):
        response = panel["client"].put("/api/topics/нет-такого/order", json={"ids": [1]})

        assert response.status_code == 404


class TestClosedWithoutLogin:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("put", "/api/topics/demo/order"),
            ("post", "/api/posts/1/redraw/1"),
            ("post", "/api/groups/demo/check"),
            ("post", "/api/groups/demo/preview"),
        ],
    )
    def test_media_endpoints_need_a_login(self, pipeline, monkeypatch, method, path):
        monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
        monkeypatch.delenv(auth.SECRET_ENV, raising=False)
        auth.set_password(PASSWORD)
        client = TestClient(create_app())

        response = getattr(client, method)(path, json={"ids": [1], "scene": "x"})

        assert response.status_code == 401
