"""Отклонение поста и возврат темы в очередь.

Снапшот в rejections — не отладочная информация, а будущий датасет: что именно
владелец не захотел публиковать. Поэтому он пишется в той же транзакции, что и
само отклонение, а не «когда-нибудь потом».
"""

import json

import pytest

from factory.core import db, machine
from factory.core.config import load_project
from factory.core.errors import FactoryError
from factory.core.models import Project, State
from factory.core.reject import reject_post
from tests.conftest import insert_post, insert_project, insert_topic


@pytest.fixture
def rejectable(conn, demo_project):
    """Проект, тема, пост в состоянии in_review с текстом и промптами."""
    config = load_project("demo")
    project_id = insert_project(conn, "demo")
    topic_id = insert_topic(conn, project_id, "Как выбрать шины")
    post_id = insert_post(
        conn, project_id, topic_id, state=State.IN_REVIEW, idem_key=f"demo:{topic_id}:0"
    )
    with db.write_transaction(conn):
        conn.execute(
            "UPDATE posts SET title = ?, body = ?, question = ?, factcheck_verdict = 'ok', "
            "state = ? WHERE id = ?",
            ("Заголовок", "Текст поста", "А у вас?", State.IN_REVIEW, post_id),
        )
        conn.execute("UPDATE topics SET status = 'taken' WHERE id = ?", (topic_id,))
        conn.execute(
            "INSERT INTO assets (post_id, kind, position, prompt, seed, created_at) "
            "VALUES (?, 'cover', 0, 'a portrait', 42, '2026-08-23T10:00:00Z')",
            (post_id,),
        )

    return {
        "conn": conn,
        "config": config,
        "project": Project.from_row(
            conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        ),
        "project_id": project_id,
        "topic_id": topic_id,
        "post_id": post_id,
    }


def state_of(conn, post_id):
    return conn.execute("SELECT state FROM posts WHERE id = ?", (post_id,)).fetchone()["state"]


def topic_status(conn, topic_id):
    return conn.execute("SELECT status FROM topics WHERE id = ?", (topic_id,)).fetchone()["status"]


class TestRejection:
    def test_post_becomes_rejected(self, rejectable):
        reject_post(rejectable["conn"], rejectable["post_id"], reason="trash")

        assert state_of(rejectable["conn"], rejectable["post_id"]) == State.REJECTED

    def test_topic_returns_to_the_queue(self, rejectable):
        reject_post(rejectable["conn"], rejectable["post_id"], reason="trash")

        assert topic_status(rejectable["conn"], rejectable["topic_id"]) == "free"

    def test_used_at_is_cleared(self, rejectable):
        """Иначе тема выглядит отработанной и статистика врёт."""
        with db.write_transaction(rejectable["conn"]):
            rejectable["conn"].execute(
                "UPDATE topics SET used_at = '2026-08-23T10:00:00Z' WHERE id = ?",
                (rejectable["topic_id"],),
            )

        reject_post(rejectable["conn"], rejectable["post_id"], reason="trash")

        used_at = rejectable["conn"].execute(
            "SELECT used_at FROM topics WHERE id = ?", (rejectable["topic_id"],)
        ).fetchone()["used_at"]
        assert used_at is None

    def test_snapshot_records_what_was_thrown_away(self, rejectable):
        reject_post(rejectable["conn"], rejectable["post_id"], reason="images")

        row = rejectable["conn"].execute("SELECT * FROM rejections").fetchone()
        snapshot = json.loads(row["snapshot"])

        assert row["reason"] == "images"
        assert snapshot["title"] == "Заголовок"
        assert snapshot["body"] == "Текст поста"
        assert snapshot["state_when_rejected"] == State.IN_REVIEW
        assert snapshot["prompts"] == [
            {"kind": "cover", "position": 0, "prompt": "a portrait", "seed": 42}
        ]

    @pytest.mark.parametrize("reason", ["text", "images", "trash"])
    def test_every_reason_from_the_spec_is_accepted(self, rejectable, reason):
        reject_post(rejectable["conn"], rejectable["post_id"], reason=reason)

        assert state_of(rejectable["conn"], rejectable["post_id"]) == State.REJECTED

    def test_unknown_reason_is_refused(self, rejectable):
        with pytest.raises(FactoryError, match="Неизвестная причина"):
            reject_post(rejectable["conn"], rejectable["post_id"], reason="не понравилось")

    def test_unknown_post_is_reported_understandably(self, rejectable):
        with pytest.raises(FactoryError) as excinfo:
            reject_post(rejectable["conn"], 999, reason="trash")

        assert "factory post list" in str(excinfo.value)

    def test_rejecting_twice_is_harmless(self, rejectable):
        """Повторное нажатие «В мусор» не должно плодить записи в rejections."""
        reject_post(rejectable["conn"], rejectable["post_id"], reason="trash")
        reject_post(rejectable["conn"], rejectable["post_id"], reason="trash")

        count = rejectable["conn"].execute("SELECT COUNT(*) FROM rejections").fetchone()[0]
        assert count == 1

    def test_published_post_cannot_be_rejected(self, rejectable):
        """Пост уже виден подписчикам — пометка в базе его оттуда не уберёт."""
        with db.write_transaction(rejectable["conn"]):
            rejectable["conn"].execute(
                "UPDATE posts SET state = ?, external_id = 'vk_1' WHERE id = ?",
                (State.PUBLISHED, rejectable["post_id"]),
            )

        with pytest.raises(FactoryError) as excinfo:
            reject_post(rejectable["conn"], rejectable["post_id"], reason="trash")

        assert "ВКонтакте" in str(excinfo.value)
        assert topic_status(rejectable["conn"], rejectable["topic_id"]) == "taken"

    def test_failed_post_can_be_rejected(self, rejectable):
        """Сломавшийся пост надо уметь выбросить, вернув тему в работу."""
        with db.write_transaction(rejectable["conn"]):
            rejectable["conn"].execute(
                "UPDATE posts SET state = ? WHERE id = ?", (State.FAILED, rejectable["post_id"])
            )

        reject_post(rejectable["conn"], rejectable["post_id"], reason="trash")

        assert topic_status(rejectable["conn"], rejectable["topic_id"]) == "free"


class TestTopicIsReused:
    def test_rejected_topic_comes_back_with_a_new_post(self, rejectable):
        """Смысл возврата темы: по ней делается вторая попытка."""
        env = rejectable
        reject_post(env["conn"], env["post_id"], reason="text")

        created = machine.replenish_queue(env["conn"], env["project"], env["config"])

        assert created >= 1
        reused = env["conn"].execute(
            "SELECT COUNT(*) FROM posts WHERE topic_id = ? AND state = ?",
            (env["topic_id"], State.QUEUED),
        ).fetchone()[0]
        assert reused == 1

    def test_new_post_gets_the_next_attempt_number(self, rejectable):
        env = rejectable
        reject_post(env["conn"], env["post_id"], reason="text")

        machine.replenish_queue(env["conn"], env["project"], env["config"])

        keys = {
            row["idem_key"]
            for row in env["conn"].execute(
                "SELECT idem_key FROM posts WHERE topic_id = ?", (env["topic_id"],)
            ).fetchall()
        }
        assert keys == {f"demo:{env['topic_id']}:0", f"demo:{env['topic_id']}:1"}

    def test_three_rejections_give_four_distinct_keys(self, rejectable):
        """Уникальный индекс не должен мешать повторным попыткам по одной теме."""
        env = rejectable

        for expected_attempt in range(1, 4):
            post_id = env["conn"].execute(
                "SELECT id FROM posts WHERE topic_id = ? AND state = ? ORDER BY id DESC LIMIT 1",
                (env["topic_id"], State.QUEUED if expected_attempt > 1 else State.IN_REVIEW),
            ).fetchone()["id"]
            reject_post(env["conn"], post_id, reason="trash")
            machine.create_post_for_next_topic(env["conn"], env["project"])

        keys = [
            row["idem_key"]
            for row in env["conn"].execute(
                "SELECT idem_key FROM posts WHERE topic_id = ? ORDER BY id", (env["topic_id"],)
            ).fetchall()
        ]
        assert keys == [f"demo:{env['topic_id']}:{i}" for i in range(4)]

    def test_rejected_post_frees_a_slot_in_the_buffer(self, rejectable):
        env = rejectable
        small = env["config"].model_copy(
            update={"limits": env["config"].limits.model_copy(update={"queue_buffer": 1})}
        )
        assert machine.replenish_queue(env["conn"], env["project"], small) == 0

        reject_post(env["conn"], env["post_id"], reason="trash")

        assert machine.replenish_queue(env["conn"], env["project"], small) == 1
