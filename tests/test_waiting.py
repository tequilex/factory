"""Живая реакция на откат: «делаю» вместо тишины.

Нажатие «Текст заново» на две-три минуты выглядело как зависание — работа шла,
но человек этого не видел и нажимал ещё раз. Теперь сразу приходит сообщение
«вернусь через пару минут», и оно убирается, когда вариант готов.

Проверяется главное свойство: обещание не должно пережить то, что обещало.
"""

import asyncio

import pytest

from factory.core import db, machine
from factory.core.config import TelegramCfg
from factory.core.decisions import Decision, apply
from factory.core.models import Post, Project, State
from factory.core.steps import handler_for
from factory.core.clock import now_utc
from factory.bot import review_bot
from tests.test_review_bot import FakeQuery, FakeUser, named

OWNER = 123456789


def waiting_id(conn, post_id):
    return conn.execute(
        "SELECT waiting_message_id FROM posts WHERE id = ?", (post_id,)
    ).fetchone()["waiting_message_id"]


@pytest.fixture
def asking(pipeline):
    project = pipeline["project"]
    pipeline["asking_project"] = project.model_copy(
        update={
            "review": project.review.model_copy(update={"mode": "telegram"}),
            "telegram": TelegramCfg(provider="stub", chat_id=OWNER, reviewers=[OWNER]),
        }
    )

    def to_review():
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )
        ctx = pipeline["context"](State.COMPOSED)
        ctx.project = pipeline["asking_project"]
        assert handler_for(State.COMPOSED)(ctx).advanced
        pipeline["context"](State.IN_REVIEW)
        return ctx

    pipeline["to_review"] = to_review
    pipeline["dispatcher"] = review_bot.build_dispatcher(
        pipeline["conn"], {"demo": pipeline["asking_project"]}
    )

    def press(data: str) -> FakeQuery:
        query = FakeQuery(data=data, from_user=FakeUser(OWNER))
        asyncio.run(named(pipeline["dispatcher"], "callback", "on_decision").callback(query))
        return query

    pipeline["press"] = press
    return pipeline


class TestTheBotPromises:
    @pytest.mark.parametrize("decision", ["txt", "scn", "img"])
    def test_a_rollback_says_how_long_to_wait(self, asking, decision):
        """Тишина на две минуты неотличима от зависания."""
        asking["to_review"]()

        query = asking["press"](f"r:{asking['post_id']}:{decision}:1")

        assert "пару минут" in query.message.answered[-1]

    @pytest.mark.parametrize("decision", ["txt", "scn", "img"])
    def test_the_promise_is_remembered_in_the_database(self, asking, decision):
        """Между откатом и результатом бота могут перезапустить.

        Номер сообщения в памяти процесса это не переживёт, и обещание
        «вернусь через пару минут» останется висеть навсегда.
        """
        asking["to_review"]()

        asking["press"](f"r:{asking['post_id']}:{decision}:1")

        assert waiting_id(asking["conn"], asking["post_id"]) is not None

    def test_approving_promises_nothing_to_clean_up(self, asking):
        """Одобрение ничего не готовит — и ожидания после него быть не должно."""
        asking["to_review"]()

        asking["press"](f"r:{asking['post_id']}:ok:1")

        assert waiting_id(asking["conn"], asking["post_id"]) is None

    def test_trashing_promises_nothing_either(self, asking):
        asking["to_review"]()

        asking["press"](f"r:{asking['post_id']}:del:1")

        assert waiting_id(asking["conn"], asking["post_id"]) is None


class TestThePromiseIsKept:
    def test_the_waiting_message_is_removed_when_the_variant_arrives(self, asking):
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        asking["press"](f"r:{post_id}:txt:1")
        promised = waiting_id(conn, post_id)

        ctx = asking["to_review"]()

        assert promised in ctx.providers.notifier.forgotten
        assert waiting_id(conn, post_id) is None

    def test_the_owner_is_left_with_the_post_not_the_promise(self, asking):
        """«Вернусь через пару минут» под уже пришедшим вариантом — мусор."""
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        asking["press"](f"r:{post_id}:txt:1")

        ctx = asking["to_review"]()

        assert len(ctx.providers.notifier.forgotten) == 1
        assert len(ctx.providers.notifier.sent) == 2, "новый вариант не пришёл"

    def test_without_a_promise_nothing_is_deleted(self, asking):
        """Первый показ поста ничего не обещал — и убирать нечего."""
        ctx = asking["to_review"]()

        assert ctx.providers.notifier.forgotten == []

    def test_a_failure_to_delete_does_not_hide_the_post(self, asking):
        """Уборка — не повод не показать то, ради чего всё делалось."""
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        asking["press"](f"r:{post_id}:txt:1")

        asking["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )
        ctx = asking["context"](State.COMPOSED)
        ctx.project = asking["asking_project"]
        ctx.providers.notifier.forget = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("Telegram недоступен")
        )

        assert handler_for(State.COMPOSED)(ctx).advanced
        assert ctx.providers.notifier.sent, "пост не показали из-за неудавшейся уборки"


class TestABrokenPostDoesNotKeepPromising:
    def test_the_promise_is_removed_when_the_post_dies(self, asking):
        """Обещание «вернусь» рядом с «пост сломался» противоречит само себе."""
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        asking["press"](f"r:{post_id}:txt:1")
        promised = waiting_id(conn, post_id)

        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET state = ?, last_error = 'сломалось' WHERE id = ?",
                (State.FAILED, post_id),
            )
        machine.check_alerts(
            conn,
            Project(
                id=asking["project_id"], slug="demo", config_path="x",
                is_active=True, created_at=now_utc(),
            ),
            asking["asking_project"],
            asking["providers"],
        )

        assert promised in asking["providers"].notifier.forgotten
        assert waiting_id(conn, post_id) is None

    def test_it_is_removed_only_once(self, asking):
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        asking["press"](f"r:{post_id}:txt:1")
        with db.write_transaction(conn):
            conn.execute("UPDATE posts SET state = ? WHERE id = ?", (State.FAILED, post_id))

        row = Project(
            id=asking["project_id"], slug="demo", config_path="x",
            is_active=True, created_at=now_utc(),
        )
        machine.check_alerts(conn, row, asking["asking_project"], asking["providers"])
        machine.check_alerts(conn, row, asking["asking_project"], asking["providers"])

        assert len(asking["providers"].notifier.forgotten) == 1


class TestWaitingIsNotAnEvent:
    """Ожидание не событие, и в ленту оно попадать не должно.

    Пост на просмотре ждёт человека сутками, а воркер смотрит на него раз в
    минуту. Каждый взгляд ложился в runs строкой «ждёт решения»: за сутки
    десять тысяч записей, и лента событий превращалась в одну повторяющуюся
    строку. Поймано на живом экране — владелец увидел тридцать одинаковых
    сообщений за пять минут.
    """

    def test_an_idle_wait_leaves_no_trace(self, pipeline):
        from factory.core.retry import tracked_call
        from factory.core.steps import waiting

        @tracked_call("in_review")
        def step(ctx):
            return waiting("жду решения владельца")

        step(pipeline["context"](State.IN_REVIEW))
        step(pipeline["context"](State.IN_REVIEW))
        step(pipeline["context"](State.IN_REVIEW))

        rows = pipeline["conn"].execute(
            "SELECT COUNT(*) FROM runs WHERE step = 'in_review'"
        ).fetchone()[0]
        assert rows == 0

    def test_a_wait_that_cost_money_is_recorded(self, pipeline):
        """Трата не должна теряться из-за того, что шаг закончился ожиданием."""
        from factory.core.retry import tracked_call
        from factory.core.steps import waiting

        @tracked_call("composed")
        def step(ctx):
            ctx.spent += 0.42
            return waiting("отправил и жду")

        step(pipeline["context"](State.COMPOSED))

        row = pipeline["conn"].execute(
            "SELECT cost_usd FROM runs WHERE step = 'composed'"
        ).fetchone()
        assert row is not None
        assert row["cost_usd"] == pytest.approx(0.42)

    def test_a_normal_step_is_still_recorded(self, pipeline):
        from factory.core.retry import tracked_call
        from factory.core.steps import advanced

        @tracked_call("queued")
        def step(ctx):
            return advanced(State.TEXT_READY)

        step(pipeline["context"](State.QUEUED))

        assert pipeline["conn"].execute(
            "SELECT COUNT(*) FROM runs WHERE step = 'queued'"
        ).fetchone()[0] == 1


class TestWaitingReasonIsVisible:
    """Подпись состояния не объясняет, почему пост стоит.

    «Рисуются картинки» выглядит как работа — и когда лимит ключа исчерпан
    тоже. Поймано живьём: владелец смотрел на пост, видел «рисуются картинки» и
    не понимал, почему картинок нет уже полчаса.
    """

    def test_the_reason_is_remembered(self, pipeline):
        machine.record_wait(
            pipeline["conn"],
            Post.from_row(pipeline["conn"].execute(
                "SELECT * FROM posts WHERE id = ?", (pipeline["post_id"],)).fetchone()),
            "Исчерпан лимит расходов ключа у провайдера картинок.",
        )

        row = pipeline["conn"].execute(
            "SELECT waiting_reason FROM posts WHERE id = ?", (pipeline["post_id"],)
        ).fetchone()
        assert "лимит" in row["waiting_reason"]

    def test_moving_on_clears_the_reason(self, pipeline):
        """Пост сдвинулся — значит то, чего он ждал, случилось."""
        post = Post.from_row(pipeline["conn"].execute(
            "SELECT * FROM posts WHERE id = ?", (pipeline["post_id"],)).fetchone())
        machine.record_wait(pipeline["conn"], post, "жду ключ")

        machine.commit_transition(pipeline["conn"], post, State.TEXT_READY)

        row = pipeline["conn"].execute(
            "SELECT waiting_reason FROM posts WHERE id = ?", (pipeline["post_id"],)
        ).fetchone()
        assert row["waiting_reason"] is None
