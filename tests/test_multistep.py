"""Продвижение постов: многошаговый тик, ожидание, backoff.

Ключевое свойство — состояние фиксируется после КАЖДОГО шага, даже внутри
цепочки. Из-за этого обрыв в середине не теряет прогресс предыдущих шагов.
"""

from datetime import timedelta

import pytest

from factory.core import db, machine
from factory.core.clock import from_iso, now_utc, to_iso
from factory.core.config import load_project
from factory.core.errors import FactoryError
from factory.core.models import Post, Project, State
from factory.core.steps import advanced, waiting
from factory.providers.registry import build_providers
from tests.conftest import insert_post, insert_project, insert_topic


@pytest.fixture
def one_post(conn, demo_project):
    """Проект demo и один пост в состоянии queued."""
    config = load_project("demo")
    project_id = insert_project(conn, "demo")
    topic_id = insert_topic(conn, project_id, "Тема")
    post_id = insert_post(conn, project_id, topic_id, idem_key=f"demo:{topic_id}:0")
    # Тема под постом обязана быть занята: в боевом коде их нельзя разделить,
    # и фикстура не должна создавать состояние, которого система не производит.
    conn.execute("UPDATE topics SET status = 'taken' WHERE id = ?", (topic_id,))
    conn.commit()

    return {
        "conn": conn,
        "config": config,
        "providers": build_providers(config),
        "project": Project.from_row(
            conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        ),
        "project_id": project_id,
        "topic_id": topic_id,
        "post_id": post_id,
        "post": lambda: machine.reload_post(conn, post_id),
    }


def advance(env, **kwargs):
    return machine.advance_post(
        env["conn"], env["post"](), env["config"], env["providers"], **kwargs
    )


class TestStepsPerTick:
    def test_one_step_moves_exactly_one_state(self, one_post):
        assert advance(one_post, max_steps=1) == 1
        assert one_post["post"]().state == State.TEXT_READY

    def test_three_steps_move_three_states(self, one_post):
        assert advance(one_post, max_steps=3) == 3
        assert one_post["post"]().state == State.PROMPTS_READY

    def test_limit_comes_from_the_environment(self, one_post, monkeypatch):
        monkeypatch.setenv("FACTORY_MAX_STEPS_PER_TICK", "2")

        assert advance(one_post) == 2
        assert one_post["post"]().state == State.FACTCHECKED

    def test_default_limit_is_three(self, one_post, monkeypatch):
        monkeypatch.delenv("FACTORY_MAX_STEPS_PER_TICK", raising=False)

        assert advance(one_post) == 3

    def test_chain_stops_at_a_terminal_state(self, one_post, monkeypatch):
        """Дойдя до published, цепочка обязана остановиться, а не искать обработчик."""
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")

        steps = advance(one_post, max_steps=50)

        assert one_post["post"]().state == State.PUBLISHED
        assert steps == 8


class TestWaiting:
    def test_waiting_breaks_the_chain_without_counting_a_failure(self, one_post, monkeypatch):
        monkeypatch.setattr(
            "factory.core.machine.handler_for", lambda state: lambda ctx: waiting("жду")
        )

        assert advance(one_post, max_steps=3) == 0

        post = one_post["post"]()
        assert post.state == State.QUEUED
        assert post.retry_count == 0
        assert post.last_error is None

    def test_waiting_post_is_revisited_next_tick(self, one_post, monkeypatch):
        monkeypatch.setenv("FACTORY_TICK_INTERVAL_SEC", "600")
        monkeypatch.setattr(
            "factory.core.machine.handler_for", lambda state: lambda ctx: waiting("жду")
        )

        advance(one_post, max_steps=3)

        post = one_post["post"]()
        assert post.next_attempt_at is not None
        assert post.next_attempt_at <= now_utc() + timedelta(seconds=601)

    def test_waiting_never_leads_to_failed(self, one_post, monkeypatch):
        """Пост в ревью может ждать человека неделю — умирать он не должен."""
        monkeypatch.setattr(
            "factory.core.machine.handler_for", lambda state: lambda ctx: waiting("жду")
        )

        for _ in range(machine.MAX_RETRIES + 3):
            with db.write_transaction(one_post["conn"]):
                one_post["conn"].execute(
                    "UPDATE posts SET next_attempt_at = NULL WHERE id = ?", (one_post["post_id"],)
                )
            advance(one_post, max_steps=1)

        post = one_post["post"]()
        assert post.state == State.QUEUED
        assert post.retry_count == 0


class TestFailures:
    def test_progress_before_a_failure_is_kept(self, one_post, monkeypatch):
        """Главный тест возобновляемости внутри цепочки."""
        real = machine.handler_for

        def failing_on_the_third(state):
            if state == State.FACTCHECKED:
                def boom(ctx):
                    raise RuntimeError("третий шаг упал")

                return boom
            return real(state)

        monkeypatch.setattr("factory.core.machine.handler_for", failing_on_the_third)

        assert advance(one_post, max_steps=3) == 2

        post = one_post["post"]()
        assert post.state == State.FACTCHECKED, "первые два перехода должны были сохраниться"
        assert post.title, "текст, полученный на первом шаге, потерян"
        assert post.retry_count == 1
        assert "третий шаг упал" in post.last_error

    def test_a_failed_step_is_not_retried_within_the_same_tick(self, one_post, monkeypatch):
        """Упавший шаг обязан оборвать цепочку, а не повторяться тут же.

        Иначе backoff не работает вовсе: за один тик пост пять раз ткнётся в одну
        и ту же ошибку и уйдёт в failed за секунды вместо нескольких часов, в
        течение которых временный сбой мог бы пройти сам.
        """
        real = machine.handler_for

        def failing_on_the_third(state):
            if state == State.FACTCHECKED:
                def boom(ctx):
                    raise RuntimeError("третий шаг упал")

                return boom
            return real(state)

        monkeypatch.setattr("factory.core.machine.handler_for", failing_on_the_third)

        assert advance(one_post, max_steps=10) == 2

        post = one_post["post"]()
        assert post.retry_count == 1, "шаг повторили в том же тике, backoff проигнорирован"
        assert post.state == State.FACTCHECKED

    def test_retry_count_grows_and_backoff_extends(self, one_post, monkeypatch):
        monkeypatch.setattr(
            "factory.core.machine.handler_for",
            lambda state: (lambda ctx: (_ for _ in ()).throw(RuntimeError("упал"))),
        )
        delays = []

        for _ in range(4):
            with db.write_transaction(one_post["conn"]):
                one_post["conn"].execute(
                    "UPDATE posts SET next_attempt_at = NULL WHERE id = ?", (one_post["post_id"],)
                )
            before = now_utc()
            advance(one_post, max_steps=1)
            post = one_post["post"]()
            delays.append(round((post.next_attempt_at - before).total_seconds() / 60))

        assert delays == [10, 20, 40, 80]

    def test_fifth_failure_gives_up(self, one_post, monkeypatch):
        monkeypatch.setattr(
            "factory.core.machine.handler_for",
            lambda state: (lambda ctx: (_ for _ in ()).throw(RuntimeError("упал"))),
        )

        for _ in range(machine.MAX_RETRIES):
            with db.write_transaction(one_post["conn"]):
                one_post["conn"].execute(
                    "UPDATE posts SET next_attempt_at = NULL WHERE id = ?", (one_post["post_id"],)
                )
            advance(one_post, max_steps=1)

        post = one_post["post"]()
        assert post.state == State.FAILED
        assert post.retry_count == machine.MAX_RETRIES
        assert post.next_attempt_at is None

    def test_the_160_minute_delay_is_never_used(self, one_post):
        """Пятый отказ уводит пост в failed, поэтому пауза 160 минут не наступает."""
        used = [machine.backoff_sec(n) // 60 for n in range(1, machine.MAX_RETRIES)]

        assert used == [10, 20, 40, 80]

    def test_backoff_is_capped_at_six_hours(self):
        assert machine.backoff_sec(99) == machine.BACKOFF_CAP_SEC == 6 * 3600

    def test_a_successful_step_clears_the_error(self, one_post):
        with db.write_transaction(one_post["conn"]):
            one_post["conn"].execute(
                "UPDATE posts SET retry_count = 3, last_error = 'старое' WHERE id = ?",
                (one_post["post_id"],),
            )

        advance(one_post, max_steps=1)

        post = one_post["post"]()
        assert post.retry_count == 0
        assert post.last_error is None

    def test_factory_error_is_stored_readably(self, one_post, monkeypatch):
        """В last_error должно попасть человекочитаемое сообщение, а не имя класса."""
        monkeypatch.setattr(
            "factory.core.machine.handler_for",
            lambda state: (
                lambda ctx: (_ for _ in ()).throw(
                    FactoryError("Не найден шаблон обложки.", what_to_do="Проверь конфиг.")
                )
            ),
        )

        advance(one_post, max_steps=1)

        assert "Не найден шаблон обложки." in one_post["post"]().last_error
        assert "Что делать: Проверь конфиг." in one_post["post"]().last_error


class TestDuePosts:
    def test_a_post_scheduled_for_later_is_skipped(self, one_post):
        future = now_utc() + timedelta(hours=1)
        with db.write_transaction(one_post["conn"]):
            one_post["conn"].execute(
                "UPDATE posts SET next_attempt_at = ? WHERE id = ?",
                (to_iso(future), one_post["post_id"]),
            )

        assert machine.due_posts(one_post["conn"], one_post["project_id"]) == []

    def test_a_post_whose_time_has_come_is_picked_up(self, one_post):
        past = now_utc() - timedelta(minutes=1)
        with db.write_transaction(one_post["conn"]):
            one_post["conn"].execute(
                "UPDATE posts SET next_attempt_at = ? WHERE id = ?",
                (to_iso(past), one_post["post_id"]),
            )

        assert len(machine.due_posts(one_post["conn"], one_post["project_id"])) == 1

    @pytest.mark.parametrize("terminal", [State.PUBLISHED, State.FAILED, State.REJECTED])
    def test_terminal_posts_are_never_picked_up(self, one_post, terminal):
        with db.write_transaction(one_post["conn"]):
            one_post["conn"].execute(
                "UPDATE posts SET state = ? WHERE id = ?", (terminal, one_post["post_id"])
            )

        assert machine.due_posts(one_post["conn"], one_post["project_id"]) == []


class TestTick:
    def test_full_tick_creates_and_advances(self, one_post, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        for i in range(2, 8):
            insert_topic(one_post["conn"], one_post["project_id"], f"Тема {i}")
        one_post["conn"].commit()

        summary = machine.tick(one_post["conn"])

        assert summary["projects"] == 1
        assert summary["posts_created"] == 5, "буфер demo — 6, один пост уже был"
        assert summary["advanced"] > 0

    def test_second_tick_in_parallel_is_skipped(self, one_post):
        conn2 = db.connect()
        try:
            with machine.lock.tick_lock(one_post["conn"]):
                assert machine.tick(conn2)["skipped"] is True
        finally:
            conn2.close()

    def test_heartbeat_is_written(self, one_post, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")

        machine.tick(one_post["conn"])

        assert machine.lock.heartbeat_age_sec(one_post["conn"]) is not None

    def test_broken_project_does_not_stop_the_others(self, one_post, monkeypatch):
        """Один битый конфиг не должен останавливать остальные группы."""
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        insert_project(one_post["conn"], "no_such_project_on_disk")
        one_post["conn"].commit()

        summary = machine.tick(one_post["conn"])

        assert summary["projects"] == 1
        assert summary["advanced"] > 0

    def test_ignore_schedule_warns_on_every_tick(self, one_post, monkeypatch, caplog):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")

        with caplog.at_level("WARNING"):
            machine.tick(one_post["conn"])

        assert any("FACTORY_IGNORE_SCHEDULE" in record.message for record in caplog.records)

    def test_no_warning_when_the_schedule_is_on(self, one_post, monkeypatch, caplog):
        monkeypatch.delenv("FACTORY_IGNORE_SCHEDULE", raising=False)

        with caplog.at_level("WARNING"):
            machine.tick(one_post["conn"])

        assert not any("FACTORY_IGNORE_SCHEDULE" in record.message for record in caplog.records)
