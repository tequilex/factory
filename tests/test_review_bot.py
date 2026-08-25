"""Бот: приём нажатий, права, ответы владельцу.

Сеть заблокирована, поэтому обработчики достаются из диспетчера и вызываются
напрямую с поддельным нажатием. Проверяется поведение, а не то, что aiogram
умеет маршрутизировать — это его работа, а не наша.
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from factory.core import db
from factory.core.clock import now_utc
from factory.core.config import TelegramCfg
from factory.core.decisions import Decision
from factory.core.models import State, TopicStatus
from factory.bot import review_bot

OWNER = 123456789
STRANGER = 111222333


@dataclass
class FakeMessage:
    """Сообщение, к которому прицеплены кнопки."""

    answered: list[str] = field(default_factory=list)
    markup_edits: int = 0
    edit_fails: bool = False
    last_markup: object = None

    async def answer(self, text: str) -> None:
        self.answered.append(text)

    async def edit_reply_markup(self, reply_markup=None) -> None:
        if self.edit_fails:
            raise RuntimeError("сообщение слишком старое")
        self.markup_edits += 1
        self.last_markup = reply_markup


@dataclass
class FakeUser:
    id: int


@dataclass
class FakeQuery:
    """Нажатие на кнопку."""

    data: str
    from_user: FakeUser
    message: FakeMessage = field(default_factory=FakeMessage)
    answers: list[tuple[str, bool]] = field(default_factory=list)

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    @property
    def said(self) -> str:
        return " ".join(text for text, _ in self.answers)


def handler_of(dispatcher, kind: str):
    """Достать обработчики нужного типа из диспетчера."""
    observer = {"callback": dispatcher.callback_query, "message": dispatcher.message}[kind]
    return observer.handlers


def named(dispatcher, kind: str, name: str):
    """Обработчик по имени функции.

    По номеру выбирать нельзя: добавление любого нового обработчика сдвигает
    остальные, и тест начинает проверять не то, что заявлено в названии.
    """
    for handler in handler_of(dispatcher, kind):
        if getattr(handler.callback, "__name__", "") == name:
            return handler
    raise AssertionError(f"обработчик {name} не найден")


def call(coro):
    return asyncio.run(coro)


@pytest.fixture
def bot_env(pipeline):
    """Пост в ревью плюс собранный диспетчер с проектом demo."""
    pipeline["advance_through"](
        State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
        State.PROMPTS_READY, State.IMAGES_READY, State.COMPOSED,
    )
    pipeline["context"](State.IN_REVIEW)
    with db.write_transaction(pipeline["conn"]):
        pipeline["conn"].execute(
            "UPDATE topics SET status = ? WHERE id = ?",
            (TopicStatus.TAKEN, pipeline["topic_id"]),
        )

    project = pipeline["project"]
    asking = project.model_copy(
        update={
            "review": project.review.model_copy(update={"mode": "telegram"}),
            "telegram": TelegramCfg(provider="stub", chat_id=OWNER, reviewers=[OWNER]),
        }
    )
    dispatcher = review_bot.build_dispatcher(pipeline["conn"], {"demo": asking})

    def press(data: str, user_id: int = OWNER) -> FakeQuery:
        query = FakeQuery(data=data, from_user=FakeUser(user_id))
        call(named(dispatcher, "callback", "on_decision").callback(query))
        return query

    pipeline["dispatcher"] = dispatcher
    pipeline["press"] = press
    return pipeline


def state_of(pipeline):
    return pipeline["conn"].execute(
        "SELECT state FROM posts WHERE id = ?", (pipeline["post_id"],)
    ).fetchone()["state"]


class TestPermissions:
    def test_a_stranger_changes_nothing(self, bot_env):
        """Бот находится поиском: без проверки кнопка доступна каждому."""
        query = bot_env["press"](f"r:{bot_env['post_id']}:ok", user_id=STRANGER)

        assert state_of(bot_env) == State.IN_REVIEW
        assert "не для вас" in query.said

    def test_a_stranger_is_told_rather_than_ignored(self, bot_env):
        """Молчание со стороны неотличимо от поломки."""
        query = bot_env["press"](f"r:{bot_env['post_id']}:ok", user_id=STRANGER)

        assert query.answers, "посторонний не получил никакого ответа"
        assert query.answers[0][1] is True, "ответ показан не заметно"

    def test_the_owner_may_press(self, bot_env):
        bot_env["press"](f"r:{bot_env['post_id']}:ok")

        assert state_of(bot_env) == State.APPROVED

    def test_a_reviewer_of_another_project_may_not(self, pipeline):
        """Право нажать даёт список того проекта, чей это пост."""
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY, State.COMPOSED,
        )
        pipeline["context"](State.IN_REVIEW)
        project = pipeline["project"]
        alien = project.model_copy(
            update={
                "slug": "чужой",
                "review": project.review.model_copy(update={"mode": "telegram"}),
                "telegram": TelegramCfg(provider="stub", chat_id=OWNER, reviewers=[OWNER]),
            }
        )
        dispatcher = review_bot.build_dispatcher(pipeline["conn"], {"чужой": alien})

        query = FakeQuery(data=f"r:{pipeline['post_id']}:ok", from_user=FakeUser(OWNER))
        call(named(dispatcher, "callback", "on_decision").callback(query))

        assert state_of(pipeline) == State.IN_REVIEW
        assert "не для вас" in query.said


class TestDecisions:
    @pytest.mark.parametrize(
        ("decision", "expected"),
        [
            (Decision.APPROVE, State.APPROVED),
            (Decision.IMAGES, State.PROMPTS_READY),
            (Decision.SCENES, State.FACTCHECKED),
            (Decision.TEXT, State.QUEUED),
            (Decision.TRASH, State.REJECTED),
        ],
    )
    def test_every_button_moves_the_post(self, bot_env, decision, expected):
        bot_env["press"](f"r:{bot_env['post_id']}:{decision.value}")

        assert state_of(bot_env) == expected

    def test_the_press_is_recorded_against_the_person(self, bot_env):
        bot_env["press"](f"r:{bot_env['post_id']}:ok")

        row = bot_env["conn"].execute(
            "SELECT decided_by FROM posts WHERE id = ?", (bot_env["post_id"],)
        ).fetchone()
        assert row["decided_by"] == OWNER

    def test_the_buttons_are_removed_after_a_decision(self, bot_env):
        """Живая клавиатура под решённым постом зовёт нажать ещё раз."""
        query = bot_env["press"](f"r:{bot_env['post_id']}:ok")

        assert query.message.markup_edits == 1

    def test_the_owner_is_told_what_happens_next(self, bot_env):
        query = bot_env["press"](f"r:{bot_env['post_id']}:txt")

        assert query.message.answered, "владельцу не сказали, что теперь будет"
        assert "заново" in query.message.answered[0]

    def test_a_second_press_is_harmless(self, bot_env):
        bot_env["press"](f"r:{bot_env['post_id']}:ok")

        query = bot_env["press"](f"r:{bot_env['post_id']}:del")

        assert state_of(bot_env) == State.APPROVED, "второе нажатие переиграло решение"
        assert "уже принято" in query.said


class TestBrokenInput:
    @pytest.mark.parametrize("data", ["r:абв:ok", "r:1:неизвестно", "r:1"])
    def test_broken_callback_data_is_refused(self, bot_env, data):
        query = bot_env["press"](data)

        assert state_of(bot_env) == State.IN_REVIEW
        assert "испорчена" in query.said

    def test_a_post_that_no_longer_exists(self, bot_env):
        query = bot_env["press"]("r:999999:ok")

        assert "не для вас" in query.said

    def test_a_failure_to_remove_buttons_does_not_undo_the_decision(self, bot_env):
        """Косметика не должна отменять применённое решение."""
        query = FakeQuery(
            data=f"r:{bot_env['post_id']}:ok",
            from_user=FakeUser(OWNER),
            message=FakeMessage(edit_fails=True),
        )

        call(named(bot_env["dispatcher"], "callback", "on_decision").callback(query))

        assert state_of(bot_env) == State.APPROVED


class TestCommands:
    def test_start_explains_the_bot(self, bot_env):
        message = FakeMessage()
        message.from_user = FakeUser(OWNER)

        call(named(bot_env["dispatcher"], "message", "on_start").callback(message))

        assert message.answered
        assert "/status" in message.answered[0]

    def test_status_shows_what_is_waiting(self, bot_env):
        message = FakeMessage()
        message.from_user = FakeUser(OWNER)

        call(named(bot_env["dispatcher"], "message", "on_status").callback(message))

        text = message.answered[0]
        assert "demo" in text
        assert "ждут вашего решения: 1" in text

    def test_status_is_refused_to_strangers(self, bot_env):
        message = FakeMessage()
        message.from_user = FakeUser(STRANGER)

        call(named(bot_env["dispatcher"], "message", "on_status").callback(message))

        assert "не для вас" in message.answered[0]

    def test_status_counts_free_topics(self, bot_env):
        """Кончились темы — публиковать будет нечего, и это надо видеть заранее."""
        from tests.conftest import insert_topic

        insert_topic(bot_env["conn"], bot_env["project_id"], "Запасная тема")
        message = FakeMessage()
        message.from_user = FakeUser(OWNER)

        call(named(bot_env["dispatcher"], "message", "on_status").callback(message))

        assert "свободных тем: 1" in message.answered[0]


class TestApprovalIsHonest:
    """Подтверждение не должно обещать того, чего не будет.

    Владелец нажимает «Опубликовать», видит бодрое «уходит в группу» и тишину:
    ключ ВК истёк, а тревога о нём уже висит и второй раз не придёт. Со стороны
    это выглядит как проглоченное нажатие.
    """

    @pytest.fixture
    def project(self, pipeline):
        from factory.core.config import TelegramCfg

        base = pipeline["project"]
        return base.model_copy(
            update={
                "review": base.review.model_copy(update={"mode": "telegram"}),
                "telegram": TelegramCfg(provider="stub", chat_id=OWNER, reviewers=[OWNER]),
                "vk": base.vk.model_copy(
                    update={"app_id": 54733282, "schedule": ["19:30", "21:00"]}
                ),
            }
        )

    def test_an_expired_key_is_named_instead_of_a_promise(self, bot_env, project):
        from factory.core import alerts

        conn = bot_env["conn"]
        alerts.raise_once(
            conn, bot_env["providers"].notifier, chat_id=OWNER,
            name="vk_token", scope="demo", text="истёк",
        )

        text = review_bot._approval_text(conn, project, "demo")

        assert "ключ" in text
        assert "oauth.vk.com/authorize" in text
        assert "слот" not in text, "обещали публикацию, которой не будет"

    def test_the_owner_is_told_not_to_press_again(self, bot_env, project):
        from factory.core import alerts

        conn = bot_env["conn"]
        alerts.raise_once(
            conn, bot_env["providers"].notifier, chat_id=OWNER,
            name="vk_token", scope="demo", text="истёк",
        )

        text = review_bot._approval_text(conn, project, "demo")

        assert "повторно" in text or "не нужно" in text

    def test_a_working_key_gives_the_exact_time(self, bot_env, project):
        """«В ближайший слот» без часа читается как «сейчас»."""
        text = review_bot._approval_text(bot_env["conn"], project, "demo")

        assert ":" in text
        assert "слот" in text

    def test_without_a_schedule_it_says_so(self, bot_env, project):
        naked = project.model_copy(update={"vk": project.vk.model_copy(update={"schedule": []})})

        text = review_bot._approval_text(bot_env["conn"], naked, "demo")

        assert "расписание не задано" in text


class TestNextSlot:
    def test_it_picks_the_coming_slot_today(self, pipeline):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from factory.core.steps.publish import next_slot_start

        base = pipeline["project"]
        project = base.model_copy(
            update={"vk": base.vk.model_copy(update={"schedule": ["19:30", "21:00"]})}
        )
        moment = datetime(2026, 8, 25, 18, 0, tzinfo=ZoneInfo("Europe/Moscow"))

        assert next_slot_start(project, moment).strftime("%d %H:%M") == "25 19:30"

    def test_after_the_last_slot_it_rolls_to_tomorrow(self, pipeline):
        """Иначе вечером бот показывал бы время, которое уже прошло."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from factory.core.steps.publish import next_slot_start

        base = pipeline["project"]
        project = base.model_copy(
            update={"vk": base.vk.model_copy(update={"schedule": ["19:30", "21:00"]})}
        )
        moment = datetime(2026, 8, 25, 23, 0, tzinfo=ZoneInfo("Europe/Moscow"))

        assert next_slot_start(project, moment).strftime("%d %H:%M") == "26 19:30"

    def test_no_schedule_no_time(self, pipeline):
        from factory.core.steps.publish import next_slot_start

        base = pipeline["project"]
        project = base.model_copy(update={"vk": base.vk.model_copy(update={"schedule": []})})

        assert next_slot_start(project, now_utc()) is None


class TestApprovalRespectsTheScheduleSwitch:
    def test_with_the_schedule_off_no_slot_is_promised(self, bot_env, monkeypatch):
        """Иначе бот называет час, до которого никто ждать не собирается."""
        from factory.core.config import TelegramCfg

        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        base = bot_env["project"]
        project = base.model_copy(
            update={
                "telegram": TelegramCfg(provider="stub", chat_id=OWNER, reviewers=[OWNER]),
                "vk": base.vk.model_copy(update={"schedule": ["19:30"], "app_id": 1}),
            }
        )

        text = review_bot._approval_text(bot_env["conn"], project, "demo")

        assert "расписание отключено" in text
        assert "19:30" not in text


class TestCancelButton:
    """Кнопки не должны исчезать бесследно: пост одобрен, но ещё не вышел."""

    def test_approving_leaves_a_way_back(self, bot_env):
        query = bot_env["press"](f"r:{bot_env['post_id']}:ok")

        markup = query.message.last_markup
        assert markup is not None, "кнопки убрали совсем — передумать нечем"
        buttons = [b for row in markup.inline_keyboard for b in row]
        assert len(buttons) == 1
        assert "Отменить" in buttons[0].text

    def test_cancelling_brings_all_the_decisions_back(self, bot_env):
        bot_env["press"](f"r:{bot_env['post_id']}:ok")

        query = bot_env["press"](f"r:{bot_env['post_id']}:back")

        assert state_of(bot_env) == State.IN_REVIEW
        buttons = [b for row in query.message.last_markup.inline_keyboard for b in row]
        assert len(buttons) == 5

    def test_the_owner_is_told_the_post_is_back(self, bot_env):
        bot_env["press"](f"r:{bot_env['post_id']}:ok")

        query = bot_env["press"](f"r:{bot_env['post_id']}:back")

        assert "отменена" in query.message.answered[-1]

    @pytest.mark.parametrize("decision", ["txt", "scn", "img"])
    def test_a_rollback_leaves_the_variant_publishable(self, bot_env, decision):
        """В этом весь смысл вариантов: посмотреть другой, не потеряв этот.

        Убрать кнопки со старого сообщения значит вернуться к тому, от чего
        уходили — выбор между вариантами снова становится невозможен.
        """
        query = bot_env["press"](f"r:{bot_env['post_id']}:{decision}:1")

        markup = query.message.last_markup
        assert markup is not None, "старый вариант остался без кнопки"
        buttons = [b for row in markup.inline_keyboard for b in row]
        assert len(buttons) == 1
        assert "Опубликовать" in buttons[0].text
        assert buttons[0].callback_data.endswith(":1"), "кнопка ведёт не на свой вариант"

    def test_trashing_leaves_no_buttons(self, bot_env):
        """Пост выброшен — публиковать нечего."""
        query = bot_env["press"](f"r:{bot_env['post_id']}:del:1")

        assert query.message.last_markup is None

    def test_the_owner_is_told_the_variant_is_kept(self, bot_env):
        query = bot_env["press"](f"r:{bot_env['post_id']}:txt:1")

        assert "никуда не делся" in query.message.answered[-1]

    def test_cancelling_a_published_post_says_why_not(self, bot_env):
        """Общее «решение уже принято» тут вводит в заблуждение."""
        bot_env["press"](f"r:{bot_env['post_id']}:ok")
        with db.write_transaction(bot_env["conn"]):
            bot_env["conn"].execute(
                "UPDATE posts SET external_id = ?, state = ? WHERE id = ?",
                ("-1_5", State.PUBLISHED, bot_env["post_id"]),
            )

        query = bot_env["press"](f"r:{bot_env['post_id']}:back")

        assert "уже вышел" in query.said
        assert "в самой группе" in query.said


class TestPublishingAnOldVariant:
    """Одобряют тот вариант, под которым нажали, а не последний сделанный."""

    @pytest.fixture
    def two_variants(self, bot_env):
        from factory.core import versions
        from factory.core.decisions import Decision, apply

        conn, post_id = bot_env["conn"], bot_env["post_id"]
        versions.record(conn, machine_post(conn, post_id))
        first = conn.execute("SELECT body FROM posts WHERE id = ?", (post_id,)).fetchone()["body"]

        apply(conn, post_id, Decision.TEXT)
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET state = ?, body = ?, version = 2 WHERE id = ?",
                (State.IN_REVIEW, "второй вариант текста", post_id),
            )
        versions.record(conn, machine_post(conn, post_id))

        bot_env["first_body"] = first
        return bot_env

    def test_approving_the_first_brings_it_back(self, two_variants):
        conn, post_id = two_variants["conn"], two_variants["post_id"]

        two_variants["press"](f"r:{post_id}:ok:1")

        row = conn.execute("SELECT state, body FROM posts WHERE id = ?", (post_id,)).fetchone()
        assert row["state"] == State.APPROVED
        assert row["body"] == two_variants["first_body"], "в группу уехал бы не тот вариант"

    def test_approving_the_latest_leaves_it_alone(self, two_variants):
        conn, post_id = two_variants["conn"], two_variants["post_id"]

        two_variants["press"](f"r:{post_id}:ok:2")

        row = conn.execute("SELECT body FROM posts WHERE id = ?", (post_id,)).fetchone()
        assert row["body"] == "второй вариант текста"

    def test_a_variant_that_no_longer_exists_changes_nothing(self, two_variants):
        """Решение не применяется целиком, а не наполовину.

        Раньше вариант восстанавливался до проверки состояния: отклонённое
        решение всё равно подменяло текст поста и номер варианта, а вместе с ним
        и папку, куда лягут следующие картинки. Первый вариант при этом
        затирался — то самое, ради предотвращения чего варианты и заведены.
        """
        conn, post_id = two_variants["conn"], two_variants["post_id"]
        before = conn.execute(
            "SELECT body, version FROM posts WHERE id = ?", (post_id,)
        ).fetchone()

        query = two_variants["press"](f"r:{post_id}:ok:99")

        assert state_of(two_variants) == State.IN_REVIEW
        after = conn.execute(
            "SELECT body, version FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        assert (after["body"], after["version"]) == (before["body"], before["version"])
        assert query.said, "владельцу не сказали, что нажатие не сработало"

    def test_a_press_on_a_post_that_moved_on_does_not_restore_anything(self, two_variants):
        """Кнопка живёт на старых сообщениях вечно и однажды будет нажата поздно."""
        conn, post_id = two_variants["conn"], two_variants["post_id"]
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET state = ?, body = ? WHERE id = ?",
                (State.QUEUED, "уже переделывается", post_id),
            )

        two_variants["press"](f"r:{post_id}:ok:1")

        row = conn.execute("SELECT body, version FROM posts WHERE id = ?", (post_id,)).fetchone()
        assert row["body"] == "уже переделывается", "вариант подменили вопреки состоянию"
        assert row["version"] == 2

    def test_an_old_button_without_a_variant_still_works(self, bot_env):
        """Сообщения живут в переписке неделями и переживают обновления."""
        bot_env["press"](f"r:{bot_env['post_id']}:ok")

        assert state_of(bot_env) == State.APPROVED


def machine_post(conn, post_id):
    from factory.core.models import Post

    return Post.from_row(conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone())


class TestApprovalRespectsTheDailyLimit:
    """Лимит про количество, а не про время — он действует и без расписания.

    Владелец нажал «Опубликовать», увидел «уходит ближайшим тиком» и ничего не
    дождался: сегодня уже вышло два поста из двух. Со стороны это неотличимо от
    поломки.
    """

    @pytest.fixture
    def project(self, bot_env):
        from factory.core.config import TelegramCfg

        base = bot_env["project"]
        return base.model_copy(
            update={
                "telegram": TelegramCfg(provider="stub", chat_id=OWNER, reviewers=[OWNER]),
                "vk": base.vk.model_copy(update={"app_id": 1, "schedule": ["19:30"]}),
            }
        )

    def spend_the_day(self, bot_env, project):
        from factory.core.clock import to_iso
        from tests.conftest import insert_post, insert_topic

        conn = bot_env["conn"]
        for number in range(project.limits.posts_per_day):
            topic = insert_topic(conn, bot_env["project_id"], f"Вышел {number}")
            post = insert_post(conn, bot_env["project_id"], topic, idem_key=f"demo:{topic}:0")
            with db.write_transaction(conn):
                conn.execute(
                    "UPDATE posts SET state = ?, published_at = ? WHERE id = ?",
                    (State.PUBLISHED, to_iso(now_utc()), post),
                )

    def test_the_owner_is_told_it_waits_until_tomorrow(self, bot_env, project):
        self.spend_the_day(bot_env, project)

        text = review_bot._approval_text(bot_env["conn"], project, "demo")

        assert "завтра" in text
        assert "posts_per_day" in text

    def test_with_room_left_the_usual_answer_comes_back(self, bot_env, project):
        text = review_bot._approval_text(bot_env["conn"], project, "demo")

        assert "завтра" not in text
