"""Модели и карта переходов."""

from datetime import datetime, timezone

import pytest

from factory.core.errors import FactoryError
from factory.core.models import (
    TERMINAL_STATES,
    TRANSITIONS,
    Asset,
    Post,
    Project,
    Rejection,
    Run,
    State,
    Topic,
    TopicStatus,
    is_terminal,
    next_state,
)
from tests.conftest import insert_post, insert_project, insert_topic


class TestStates:
    def test_terminal_states_are_exactly_three(self):
        assert TERMINAL_STATES == frozenset({State.PUBLISHED, State.FAILED, State.REJECTED})

    def test_state_is_a_plain_string_for_the_database(self):
        assert State.QUEUED == "queued"
        assert f"{State.COMPOSED}" == "composed"

    def test_the_chain_runs_from_queued_to_published(self):
        chain = [State.QUEUED]
        while not is_terminal(chain[-1]):
            chain.append(next_state(chain[-1]))

        assert chain == [
            State.QUEUED,
            State.TEXT_READY,
            State.FACTCHECKED,
            State.PROMPTS_READY,
            State.IMAGES_READY,
            State.COMPOSED,
            State.IN_REVIEW,
            State.APPROVED,
            State.PUBLISHED,
        ]

    def test_every_non_terminal_state_has_a_successor(self):
        for state in State:
            assert is_terminal(state) or state in TRANSITIONS

    def test_no_terminal_state_has_a_successor(self):
        assert TERMINAL_STATES.isdisjoint(TRANSITIONS)

    def test_next_state_of_a_terminal_state_is_refused(self):
        with pytest.raises(FactoryError, match="терминальное"):
            next_state(State.PUBLISHED)

    def test_next_state_of_an_unknown_state_explains_itself(self):
        with pytest.raises(FactoryError) as excinfo:
            next_state("text_redy")

        assert "text_redy" in str(excinfo.value)


class TestPost:
    def test_from_row_fills_every_field(self, conn):
        project_id = insert_project(conn)
        topic_id = insert_topic(conn, project_id)
        post_id = insert_post(conn, project_id, topic_id, state=State.COMPOSED)
        conn.execute(
            "UPDATE posts SET title = ?, body = ?, question = ?, retry_count = 2, "
            "last_error = ?, external_id = ?, published_at = ? WHERE id = ?",
            ("Заголовок", "Текст", "Вопрос?", "таймаут", "wall-1_2", "2026-08-23T19:30:00Z", post_id),
        )
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()

        post = Post.from_row(row)

        assert post.id == post_id
        assert post.project_id == project_id
        assert post.topic_id == topic_id
        assert post.idem_key == f"demo:{topic_id}:0"
        assert post.state == State.COMPOSED
        assert post.title == "Заголовок"
        assert post.body == "Текст"
        assert post.question == "Вопрос?"
        assert post.retry_count == 2
        assert post.last_error == "таймаут"
        assert post.external_id == "wall-1_2"

    def test_timestamps_come_back_as_aware_datetimes(self, conn):
        project_id = insert_project(conn)
        topic_id = insert_topic(conn, project_id)
        post_id = insert_post(conn, project_id, topic_id)
        conn.execute(
            "UPDATE posts SET published_at = ? WHERE id = ?",
            ("2026-08-23T19:30:00Z", post_id),
        )
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()

        post = Post.from_row(row)

        assert post.published_at == datetime(2026, 8, 23, 19, 30, tzinfo=timezone.utc)
        assert post.created_at.tzinfo is not None
        assert post.next_attempt_at is None

    def test_is_terminal_reflects_the_state(self, conn):
        project_id = insert_project(conn)
        topic_id = insert_topic(conn, project_id)
        row = conn.execute(
            "SELECT * FROM posts WHERE id = ?",
            (insert_post(conn, project_id, topic_id, state=State.PUBLISHED),),
        ).fetchone()

        assert Post.from_row(row).is_terminal


class TestOtherModels:
    def test_project(self, conn):
        insert_project(conn, "demo")
        row = conn.execute("SELECT * FROM projects").fetchone()

        project = Project.from_row(row)

        assert project.slug == "demo"
        assert project.is_active is True
        assert project.created_at.tzinfo is not None

    def test_topic(self, conn):
        project_id = insert_project(conn)
        insert_topic(conn, project_id, "Как выбрать шины")
        row = conn.execute("SELECT * FROM topics").fetchone()

        topic = Topic.from_row(row)

        assert topic.title == "Как выбрать шины"
        assert topic.status == TopicStatus.FREE
        assert topic.used_at is None

    def test_asset(self, conn):
        project_id = insert_project(conn)
        topic_id = insert_topic(conn, project_id)
        post_id = insert_post(conn, project_id, topic_id)
        conn.execute(
            "INSERT INTO assets (post_id, kind, position, prompt, seed, created_at) "
            "VALUES (?, 'cover', 0, 'a portrait', 42, ?)",
            (post_id, "2026-08-23T10:00:00Z"),
        )
        row = conn.execute("SELECT * FROM assets").fetchone()

        asset = Asset.from_row(row)

        assert asset.kind == "cover"
        assert asset.seed == 42
        assert asset.local_path is None

    def test_run(self, conn):
        conn.execute(
            "INSERT INTO runs (step, ok, duration_ms, cost_usd, created_at) "
            "VALUES ('text_ready', 1, 1200, 0.02, ?)",
            ("2026-08-23T10:00:00Z",),
        )
        row = conn.execute("SELECT * FROM runs").fetchone()

        run = Run.from_row(row)

        assert run.ok is True
        assert run.duration_ms == 1200
        assert run.cost_usd == pytest.approx(0.02)
        assert run.post_id is None

    def test_rejection(self, conn):
        project_id = insert_project(conn)
        topic_id = insert_topic(conn, project_id)
        post_id = insert_post(conn, project_id, topic_id)
        conn.execute(
            "INSERT INTO rejections (post_id, reason, snapshot, created_at) "
            "VALUES (?, 'trash', ?, ?)",
            (post_id, '{"title": "старый"}', "2026-08-23T10:00:00Z"),
        )
        row = conn.execute("SELECT * FROM rejections").fetchone()

        rejection = Rejection.from_row(row)

        assert rejection.reason == "trash"
        assert rejection.snapshot == '{"title": "старый"}'


def test_states_in_code_match_the_check_constraint_in_the_database(conn):
    """Если списки разъедутся, шаг молча упрётся в IntegrityError на боевой базе."""
    project_id = insert_project(conn)
    topic_id = insert_topic(conn, project_id)

    for index, state in enumerate(State):
        insert_post(conn, project_id, topic_id, state=state, idem_key=f"demo:{topic_id}:{index}")

    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == len(State)
