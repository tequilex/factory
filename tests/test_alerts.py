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
        # Это единственное сообщение, где владельцу надо действовать. Команда
        # для терминала в нём — инструкция, которую он выполнить не может:
        # он в телефоне, и темы бот умеет принимать сообщением.
        assert "factory" not in message
        assert "пришлите" in message

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
        # Раньше здесь стояла команда для терминала — то есть владельцу,
        # который живёт в телефоне, предлагалось сделать невозможное.
        assert "кнопкой ниже" in message
        assert watched["providers"].notifier.alert_fix_posts[-1] == watched["post_id"], (
            "кнопки починки не пришло"
        )

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


class TestAlertsNeverBreakTheTick:
    """Уведомление о поломке не имеет права стать поломкой само.

    Провайдер отдаёт наружу и httpx-исключения, а сеть до Telegram отвечает
    неровно — это записано в CLAUDE.md как известная проблема. Если такое
    исключение уйдёт из тика, не запишется хартбит, и на Этапе 7 healthcheck
    начнёт перезапускать контейнер по кругу.
    """

    def test_a_network_failure_is_swallowed(self, conn):
        import httpx

        class Dead:
            def alert(self, **kwargs):
                raise httpx.ConnectError("сеть недоступна")

        assert alerts.raise_once(
            conn, Dead(), chat_id=OWNER, name="vk_token", scope="demo", text="истёк"
        ) is False

    def test_check_alerts_survives_a_dead_notifier(self, watched):
        import httpx

        class Dead:
            def alert(self, **kwargs):
                raise httpx.ReadTimeout("не дождались")

        watched["providers"].notifier.alert = Dead().alert
        drain(watched["conn"])

        machine.check_alerts(
            watched["conn"], watched["row"], watched["watched_project"], watched["providers"]
        )

    def test_the_tick_still_writes_its_heartbeat(self, watched):
        """Главное следствие: тик доходит до конца и отмечается живым."""
        import httpx

        from factory.core import lock

        class Dead:
            def alert(self, **kwargs):
                raise httpx.ReadTimeout("не дождались")

        watched["providers"].notifier.alert = Dead().alert
        drain(watched["conn"])
        machine.check_alerts(
            watched["conn"], watched["row"], watched["watched_project"], watched["providers"]
        )
        lock.write_heartbeat(watched["conn"])

        assert lock.heartbeat_age_sec(watched["conn"]) is not None


class TestApprovedIsNotStuck:
    def test_a_post_waiting_for_its_slot_is_not_reported(self, watched):
        """Одобренный пост ждёт слота — это работа, а не застревание.

        При queue_buffer = posts_per_day × 3 владелец одобряет за один заход
        больше постов, чем выходит за сутки. Тревога на них была бы ровно тем
        шумом, из-за которого отказались от алерта «N постов ждут ревью».
        """
        watched["context"](State.APPROVED)
        age(watched["conn"], watched["post_id"], alerts.STUCK_AFTER_HOURS * 3)

        assert watched["check"]() == []


class TestTheSecondGuard:
    """Второй слой защиты: тревога может сломаться и после успешной отправки.

    ``raise_once`` глотает сбои провайдера, но сама пишет в базу, и эта запись
    тоже может не удаться. Шаг, упавший с ошибкой, зовёт тревогу прямо из
    обработчика ``except`` — без второго guard такая ошибка вышла бы из тика.
    """

    def test_a_broken_alert_does_not_break_the_step_failure_path(self, watched, monkeypatch):
        from factory.core.errors import FactoryError
        from factory.core.steps import handler_for

        conn = watched["conn"]
        watched["context"](State.APPROVED)

        def explode(*args, **kwargs):
            raise RuntimeError("база заблокирована")

        monkeypatch.setattr(alerts, "raise_once", explode)

        class ExpiredKey(FactoryError):
            token_expired = True
            token_env = "VK_UPLOAD_TOKEN"

        def failing(ctx):
            raise ExpiredKey("ключ истёк")

        monkeypatch.setitem(
            __import__("factory.core.steps", fromlist=["REGISTRY"]).REGISTRY,
            State.APPROVED,
            failing,
        )

        post = machine.reload_post(conn, watched["post_id"])
        # Не должно поднять RuntimeError наружу: тик обязан дожить до хартбита.
        machine.advance_post(conn, post, watched["watched_project"], watched["providers"])

        # Пост при этом обработан как положено: ждёт ключа, а не считает попытки.
        after = machine.reload_post(conn, watched["post_id"])
        assert after.state == State.APPROVED
        assert after.next_attempt_at is not None


class TestAlertsDoNotOverwriteEachOther:
    def test_two_different_alerts_for_one_project_both_arrive(self, watched):
        """Иначе одна тревога гасила бы другую, и о второй беде не узнают.

        Обе с одинаковой областью — именем проекта. Разойдись они только по
        области, подмена ключа осталась бы незамеченной.
        """
        conn = watched["conn"]
        alerts.raise_once(
            conn, watched["providers"].notifier, chat_id=OWNER,
            name="vk_token", scope="demo", text="ключ ВК истёк",
        )
        drain(conn)

        messages = watched["check"]()

        assert any("ключ ВК истёк" in text for text in messages)
        assert any("публиковать будет нечего" in text for text in messages)


class TestStuckThreshold:
    def test_the_threshold_is_a_day(self):
        """Число из задолженности Этапа 5: «дольше суток».

        Литералом, а не через константу: сверять константу с самой собой значит
        не проверять ничего — правка сдвинет обе стороны равенства сразу.
        """
        assert alerts.STUCK_AFTER_HOURS == 24

    def test_twelve_hours_of_waiting_is_still_normal(self, watched):
        watched["context"](State.IN_REVIEW)
        age(watched["conn"], watched["post_id"], 12)

        assert watched["check"]() == []


class TestExpiredKeyIsWaitingNotFailure:
    """Истёкший ключ — ожидание человека, а не поломка поста.

    Ключ не станет действительным сам, поэтому пять попыток просто сжигают
    бюджет: пост умирает за час, пока владелец спит, хотя достаточно было
    дождаться нового ключа. Ровно для этого и существует WAITING.
    """

    @pytest.fixture
    def expired(self, watched, monkeypatch):
        from factory.core.errors import FactoryError
        from factory.core.steps import REGISTRY

        class ExpiredKey(FactoryError):
            token_expired = True
            token_env = "VK_UPLOAD_TOKEN"

        def failing(ctx):
            raise ExpiredKey("ключ истёк")

        watched["context"](State.APPROVED)
        monkeypatch.setitem(REGISTRY, State.APPROVED, failing)
        return watched

    def run_once(self, watched):
        post = machine.reload_post(watched["conn"], watched["post_id"])
        machine.advance_post(
            watched["conn"], post, watched["watched_project"], watched["providers"]
        )
        return machine.reload_post(watched["conn"], watched["post_id"])

    def test_the_retry_budget_is_untouched(self, expired):
        for _ in range(6):
            post = self.run_once(expired)

        assert post.retry_count == 0, "ожидание человека сожгло попытки"

    def test_the_post_does_not_die(self, expired):
        for _ in range(6):
            post = self.run_once(expired)

        assert post.state == State.APPROVED, "пост умер, ожидая владельца"

    def test_the_owner_is_still_called(self, expired):
        """Молча ждать нельзя: без ключа никто ничего не сделает."""
        self.run_once(expired)

        assert any("ключ" in text for text in expired["providers"].notifier.alerts)

    def test_it_is_retried_later(self, expired):
        post = self.run_once(expired)

        assert post.next_attempt_at is not None

    def test_ordinary_failures_still_count(self, watched, monkeypatch):
        """Обратная половина: обычная поломка обязана расходовать попытки."""
        from factory.core.errors import FactoryError
        from factory.core.steps import REGISTRY

        watched["context"](State.APPROVED)
        monkeypatch.setitem(
            REGISTRY, State.APPROVED,
            lambda ctx: (_ for _ in ()).throw(FactoryError("что-то другое")),
        )

        post = machine.reload_post(watched["conn"], watched["post_id"])
        machine.advance_post(
            watched["conn"], post, watched["watched_project"], watched["providers"]
        )

        assert machine.reload_post(watched["conn"], watched["post_id"]).retry_count == 1
