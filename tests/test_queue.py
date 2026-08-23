"""Пополнение очереди постов.

Наполнение очереди и публикация развязаны намеренно: queue_buffer отвечает за
то, сколько постов одновременно в работе, posts_per_day — за то, сколько уходит
в группу за сутки. Иначе очередь разъезжается с расписанием при отложенном ревью.
"""

import pytest

from factory.core import db, machine
from factory.core.config import load_project
from factory.core.models import Project, State
from tests.conftest import insert_post, insert_project, insert_topic


@pytest.fixture
def project_with_topics(conn, demo_project):
    """Проект demo и десять свободных тем."""
    config = load_project("demo")
    project_id = insert_project(conn, "demo")
    topic_ids = [insert_topic(conn, project_id, f"Тема {i}") for i in range(1, 11)]
    conn.commit()
    project = Project.from_row(
        conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    )
    return {
        "conn": conn,
        "config": config,
        "project": project,
        "project_id": project_id,
        "topic_ids": topic_ids,
    }


def with_buffer(config, size: int):
    return config.model_copy(update={"limits": config.limits.model_copy(update={"queue_buffer": size})})


def count_posts(conn, **where) -> int:
    if not where:
        return conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    clause = " AND ".join(f"{key} = ?" for key in where)
    return conn.execute(f"SELECT COUNT(*) FROM posts WHERE {clause}", tuple(where.values())).fetchone()[0]


def topics_with_status(conn, status: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM topics WHERE status = ?", (status,)).fetchone()[0]


class TestReplenish:
    def test_fills_up_to_the_buffer(self, project_with_topics):
        """Размер берётся из конфига, а не зашит в код."""
        env = project_with_topics
        created = machine.replenish_queue(env["conn"], env["project"], with_buffer(env["config"], 3))

        assert created == 3
        assert count_posts(env["conn"]) == 3
        assert topics_with_status(env["conn"], "taken") == 3

    def test_buffer_of_six_from_the_demo_config(self, project_with_topics):
        env = project_with_topics
        machine.replenish_queue(env["conn"], env["project"], env["config"])

        assert count_posts(env["conn"]) == 6

    def test_second_call_creates_nothing(self, project_with_topics):
        env = project_with_topics
        machine.replenish_queue(env["conn"], env["project"], with_buffer(env["config"], 3))

        assert machine.replenish_queue(env["conn"], env["project"], with_buffer(env["config"], 3)) == 0
        assert count_posts(env["conn"]) == 3

    @pytest.mark.parametrize("terminal", [State.PUBLISHED, State.FAILED, State.REJECTED])
    def test_terminal_posts_free_up_a_slot(self, project_with_topics, terminal):
        env = project_with_topics
        config = with_buffer(env["config"], 3)
        machine.replenish_queue(env["conn"], env["project"], config)
        with db.write_transaction(env["conn"]):
            env["conn"].execute("UPDATE posts SET state = ? WHERE id = 1", (terminal,))

        created = machine.replenish_queue(env["conn"], env["project"], config)

        assert created == 1
        assert count_posts(env["conn"]) == 4

    def test_posts_waiting_for_review_still_occupy_the_buffer(self, project_with_topics):
        """Иначе система нагенерит постов, пока владелец в отпуске, и сожжёт деньги."""
        env = project_with_topics
        config = with_buffer(env["config"], 3)
        machine.replenish_queue(env["conn"], env["project"], config)
        with db.write_transaction(env["conn"]):
            env["conn"].execute("UPDATE posts SET state = ?", (State.IN_REVIEW,))

        assert machine.replenish_queue(env["conn"], env["project"], config) == 0

    def test_running_out_of_topics_is_not_an_error(self, project_with_topics):
        env = project_with_topics
        config = with_buffer(env["config"], 50)

        created = machine.replenish_queue(env["conn"], env["project"], config)

        assert created == 10
        assert topics_with_status(env["conn"], "free") == 0

    def test_no_topics_at_all_creates_nothing(self, conn, demo_project):
        project_id = insert_project(conn, "demo")
        conn.commit()
        project = Project.from_row(
            conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        )

        assert machine.replenish_queue(conn, project, load_project("demo")) == 0

    def test_projects_do_not_take_each_other_topics(self, project_with_topics):
        env = project_with_topics
        other_id = insert_project(env["conn"], "other")
        insert_topic(env["conn"], other_id, "Чужая тема")
        env["conn"].commit()

        machine.replenish_queue(env["conn"], env["project"], with_buffer(env["config"], 50))

        other_topic_status = env["conn"].execute(
            "SELECT status FROM topics WHERE project_id = ?", (other_id,)
        ).fetchone()["status"]
        assert other_topic_status == "free"


class TestIdemKey:
    def test_format_for_a_fresh_topic(self, project_with_topics):
        env = project_with_topics
        machine.replenish_queue(env["conn"], env["project"], with_buffer(env["config"], 1))

        row = env["conn"].execute("SELECT idem_key, topic_id FROM posts").fetchone()
        assert row["idem_key"] == f"demo:{row['topic_id']}:0"

    def test_attempt_grows_after_a_rejection(self, project_with_topics):
        """Отклонённая тема возвращается в работу — ключ должен стать уникальным."""
        env = project_with_topics
        conn = env["conn"]
        topic_id = env["topic_ids"][0]
        post_id = insert_post(conn, env["project_id"], topic_id, state=State.REJECTED,
                              idem_key=f"demo:{topic_id}:0")
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO rejections (post_id, reason, created_at) VALUES (?, 'trash', ?)",
                (post_id, "2026-08-23T10:00:00Z"),
            )
            conn.execute("UPDATE topics SET status = 'free' WHERE id = ?", (topic_id,))

        assert machine.attempts_for_topic(conn, topic_id) == 1

        machine.replenish_queue(conn, env["project"], with_buffer(env["config"], 2))

        keys = {row["idem_key"] for row in conn.execute("SELECT idem_key FROM posts").fetchall()}
        assert f"demo:{topic_id}:1" in keys


class TestClaimTopic:
    def test_returns_a_topic_and_marks_it_taken(self, project_with_topics):
        env = project_with_topics
        topic_id = machine.claim_free_topic(env["conn"], env["project_id"])

        assert topic_id in env["topic_ids"]
        status = env["conn"].execute(
            "SELECT status FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()["status"]
        assert status == "taken"

    def test_same_topic_is_never_handed_out_twice(self, project_with_topics):
        env = project_with_topics
        claimed = [machine.claim_free_topic(env["conn"], env["project_id"]) for _ in range(10)]

        assert len(set(claimed)) == 10

    def test_returns_none_when_nothing_is_free(self, project_with_topics):
        env = project_with_topics
        for _ in range(10):
            machine.claim_free_topic(env["conn"], env["project_id"])

        assert machine.claim_free_topic(env["conn"], env["project_id"]) is None

    def test_used_topics_are_not_reclaimed(self, project_with_topics):
        env = project_with_topics
        with db.write_transaction(env["conn"]):
            env["conn"].execute("UPDATE topics SET status = 'used'")

        assert machine.claim_free_topic(env["conn"], env["project_id"]) is None


class TestTopicAndPostAreCreatedTogether:
    """Захват темы и создание поста — одна транзакция, а не две.

    Если разделить, краш между ними оставит тему в статусе taken без поста, и
    вернуть её в работу будет нечем: тема потеряна навсегда.
    """

    def test_both_happen(self, project_with_topics):
        env = project_with_topics
        post_id = machine.create_post_for_next_topic(env["conn"], env["project"])

        assert post_id is not None
        assert count_posts(env["conn"]) == 1
        assert topics_with_status(env["conn"], "taken") == 1

    def test_neither_happens_when_the_insert_fails(self, project_with_topics, monkeypatch):
        """Симулируем сбой на вставке поста: тема обязана остаться свободной."""
        env = project_with_topics
        free_before = topics_with_status(env["conn"], "free")

        def explode(conn, topic_id):
            raise RuntimeError("сбой между захватом темы и вставкой поста")

        monkeypatch.setattr(machine, "attempts_for_topic", explode)

        with pytest.raises(RuntimeError):
            machine.create_post_for_next_topic(env["conn"], env["project"])

        assert count_posts(env["conn"]) == 0
        assert topics_with_status(env["conn"], "free") == free_before, "тема потеряна"

    def test_returns_none_when_no_topic_is_free(self, conn, demo_project):
        project_id = insert_project(conn, "demo")
        conn.commit()
        project = Project.from_row(
            conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        )

        assert machine.create_post_for_next_topic(conn, project) is None
