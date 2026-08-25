"""Тревоги: когда система встала и сама не выберется.

Проверяется не только «сообщение ушло», но и обратное — что оно **не** уходит
там, где всё в порядке. Тревога, которая звучит на рабочей ситуации, за неделю
приучает не читать тревоги вообще, и тогда настоящую поломку никто не заметит.
"""

import pytest

from factory.core import alerts, db, machine
from factory.core.clock import now_utc, to_iso
from factory.core.config import TelegramCfg
from factory.core.models import Project, State
from tests.conftest import insert_post, insert_topic

OWNER = 123456789


@pytest.fixture
def watched(pipeline):
    """Проект с настроенным Telegram и заглушкой уведомлений."""
    project = pipeline["project"]
    pipeline["watched_project"] = project.model_copy(
        update={
            "review": project.review.model_copy(update={"mode": "telegram"}),
            "telegram": TelegramCfg(provider="stub", chat_id=OWNER, reviewers=[OWNER]),
        }
    )
    pipeline["row"] = Project(
        id=pipeline["project_id"], slug="demo", config_path="x", is_active=True,
        created_at=now_utc(),
    )

    def check():
        machine.check_alerts(
            pipeline["conn"], pipeline["row"], pipeline["watched_project"], pipeline["providers"]
        )
        return pipeline["providers"].notifier.alerts

    pipeline["check"] = check
    return pipeline


def drain(conn):
    """Ни свободных тем, ни постов в работе — очередь действительно пуста."""
    with db.write_transaction(conn):
        conn.execute("UPDATE topics SET status = 'used'")
        conn.execute("UPDATE posts SET state = ?", (State.PUBLISHED,))


def age(conn, post_id, hours):
    """Сделать вид, что пост не двигался столько часов."""
    from datetime import timedelta

    with db.write_transaction(conn):
        conn.execute(
            "UPDATE posts SET updated_at = ? WHERE id = ?",
            (to_iso(now_utc() - timedelta(hours=hours)), post_id),
        )


class TestNothingToPublish:
    def test_silence_while_there_are_topics(self, watched):
        with db.write_transaction(watched["conn"]):
            watched["conn"].execute("UPDATE posts SET state = ?", (State.PUBLISHED,))

        assert watched["check"]() == []

    def test_silence_while_posts_are_still_in_flight(self, watched):
        """Темы кончились, но в работе достаточно постов — это не срочность."""
        conn = watched["conn"]
        with db.write_transaction(conn):
            conn.execute("UPDATE topics SET status = 'used'")
        for number in range(watched["watched_project"].limits.posts_per_day):
            topic = insert_topic(conn, watched["project_id"], f"Т{number}")
            insert_post(conn, watched["project_id"], topic, idem_key=f"demo:{topic}:0")
            with db.write_transaction(conn):
                conn.execute("UPDATE topics SET status = 'taken' WHERE id = ?", (topic,))

        assert watched["check"]() == []

    def test_the_owner_is_called_when_the_queue_runs_dry(self, watched):
        drain(watched["conn"])

        (message,) = watched["check"]()

        assert "публиковать будет нечего" in message
        assert "factory topics import demo" in message

    def test_it_does_not_repeat_every_tick(self, watched):
        """Тик идёт раз в минуту — повтор превратил бы это в поток."""
        drain(watched["conn"])
        watched["check"]()

        assert len(watched["check"]()) == 1

    def test_adding_topics_lets_it_sound_again_later(self, watched):
        """Тревога снимается вместе с причиной, иначе прозвучит только раз в жизни."""
        drain(watched["conn"])
        assert len(watched["check"]()) == 1

        insert_topic(watched["conn"], watched["project_id"], "Новая тема")
        assert len(watched["check"]()) == 1, "тревога повторилась при живой очереди"

        drain(watched["conn"])

        assert len(watched["check"]()) == 2


class TestStuckPosts:
    def test_a_fresh_post_is_not_stuck(self, watched):
        assert watched["check"]() == []

    def test_a_post_waiting_a_day_is_reported(self, watched):
        watched["context"](State.IN_REVIEW)
        age(watched["conn"], watched["post_id"], alerts.STUCK_AFTER_HOURS + 1)

        messages = watched["check"]()

        assert any("ждёт вашего решения" in text for text in messages)

    def test_just_under_a_day_stays_silent(self, watched):
        """Граница: сутки минус час — ещё не повод писать."""
        watched["context"](State.IN_REVIEW)
        age(watched["conn"], watched["post_id"], alerts.STUCK_AFTER_HOURS - 1)

        assert watched["check"]() == []

    def test_a_stuck_post_is_not_killed(self, watched):
        """Ожидание человека — не ошибка. В failed такой пост не переводится."""
        watched["context"](State.IN_REVIEW)
        age(watched["conn"], watched["post_id"], alerts.STUCK_AFTER_HOURS + 50)

        watched["check"]()

        state = watched["conn"].execute(
            "SELECT state FROM posts WHERE id = ?", (watched["post_id"],)
        ).fetchone()["state"]
        assert state == State.IN_REVIEW

    def test_it_is_reported_once_per_post(self, watched):
        watched["context"](State.IN_REVIEW)
        age(watched["conn"], watched["post_id"], alerts.STUCK_AFTER_HOURS + 1)
        first = len(watched["check"]())

        assert len(watched["check"]()) == first

    def test_each_stuck_post_gets_its_own_message(self, watched):
        """Одна тревога на всех означала бы, что о втором посте не узнают никогда."""
        conn = watched["conn"]
        topic = insert_topic(conn, watched["project_id"], "Вторая")
        second = insert_post(conn, watched["project_id"], topic, idem_key=f"demo:{topic}:0")
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET state = ? WHERE id IN (?, ?)",
                (State.IN_REVIEW, watched["post_id"], second),
            )
        age(conn, watched["post_id"], alerts.STUCK_AFTER_HOURS + 1)
        age(conn, second, alerts.STUCK_AFTER_HOURS + 1)

        messages = watched["check"]()

        assert len(messages) == 2
        assert any(f"Пост {second}" in text for text in messages)

    def test_a_published_post_is_never_stuck(self, watched):
        """Терминальные состояния не двигаются по определению."""
        watched["context"](State.PUBLISHED)
        age(watched["conn"], watched["post_id"], alerts.STUCK_AFTER_HOURS * 10)

        assert not any("застрял" in text or "ждёт" in text for text in watched["check"]())


class TestFailedPosts:
    def test_the_owner_is_told_with_the_reason(self, watched):
        with db.write_transaction(watched["conn"]):
            watched["conn"].execute(
                "UPDATE posts SET state = ?, last_error = ? WHERE id = ?",
                (State.FAILED, "ВКонтакте отказал: ошибка 5", watched["post_id"]),
            )

        (message,) = watched["check"]()

        assert "сломался" in message
        assert "ошибка 5" in message, "владельцу не сказали причину"
        assert f"factory post retry {watched['post_id']}" in message

    def test_it_is_reported_once(self, watched):
        with db.write_transaction(watched["conn"]):
            watched["conn"].execute(
                "UPDATE posts SET state = ? WHERE id = ?", (State.FAILED, watched["post_id"])
            )
        watched["check"]()

        assert len(watched["check"]()) == 1

    def test_each_broken_post_gets_its_own_message(self, watched):
        conn = watched["conn"]
        topic = insert_topic(conn, watched["project_id"], "Вторая")
        second = insert_post(conn, watched["project_id"], topic, idem_key=f"demo:{topic}:0")
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET state = ? WHERE id IN (?, ?)",
                (State.FAILED, watched["post_id"], second),
            )

        assert len(watched["check"]()) == 2


class TestQuietProjects:
    def test_a_project_without_telegram_is_not_alerted(self, pipeline):
        """Режим auto — владельца ни о чём не спрашивают и не тревожат."""
        row = Project(
            id=pipeline["project_id"], slug="demo", config_path="x", is_active=True,
            created_at=now_utc(),
        )
        with db.write_transaction(pipeline["conn"]):
            pipeline["conn"].execute("UPDATE posts SET state = ?", (State.FAILED,))

        machine.check_alerts(pipeline["conn"], row, pipeline["project"], pipeline["providers"])

        assert pipeline["providers"].notifier.alerts == []
