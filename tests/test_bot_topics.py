"""Темы, пауза и починка — из телефона, а не из терминала.

До этого бот в нескольких местах советовал выполнить команду: «factory topics
import», «factory post retry». Владелец работает с телефона, и такой совет —
тупик. Здесь проверяется, что каждое такое действие делается прямо в переписке.
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from factory.core import alerts, db, topics
from factory.core.config import TelegramCfg
from factory.core.decisions import Decision, apply
from factory.core.models import State, TopicStatus
from factory.bot import review_bot
from tests.test_review_bot import FakeQuery, FakeUser, named

OWNER = 123456789
STRANGER = 111222333


@dataclass
class FakeMessage:
    text: str = ""
    from_user: object = None
    message_id: int = 900
    answered: list[str] = field(default_factory=list)
    markups: list[object] = field(default_factory=list)

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answered.append(text)
        self.markups.append(reply_markup)


@pytest.fixture
def bot(pipeline):
    project = pipeline["project"]
    asking = project.model_copy(
        update={
            "review": project.review.model_copy(update={"mode": "telegram"}),
            "telegram": TelegramCfg(provider="stub", chat_id=OWNER, reviewers=[OWNER]),
        }
    )
    dispatcher = review_bot.build_dispatcher(pipeline["conn"], {"demo": asking})

    def send(name: str, text: str = "", user: int = OWNER) -> FakeMessage:
        message = FakeMessage(text=text, from_user=FakeUser(user))
        asyncio.run(named(dispatcher, "message", name).callback(message))
        return message

    def press(data: str = "t:add:900", user: int = OWNER) -> FakeQuery:
        query = FakeQuery(data=data, from_user=FakeUser(user))
        asyncio.run(named(dispatcher, "callback", "on_topics_answer").callback(query))
        return query

    pipeline["send"] = send
    pipeline["press_topics"] = press
    pipeline["dispatcher"] = dispatcher
    return pipeline


def free_titles(conn, project_id):
    return [row["title"] for row in conn.execute(
        "SELECT title FROM topics WHERE project_id = ? AND status = ? ORDER BY id",
        (project_id, TopicStatus.FREE),
    ).fetchall()]


class TestTopicsCommand:
    def test_it_shows_the_counts(self, bot):
        message = bot["send"]("on_topics")

        text = message.answered[0]
        assert "свободных: 1" in text
        assert "в работе: 0" in text

    def test_it_lists_what_is_coming(self, bot):
        topics.add(bot["conn"], bot["project_id"], ["Первая", "Вторая"])

        text = bot["send"]("on_topics").answered[0]

        assert "Первая" in text
        assert "Вторая" in text

    def test_an_empty_queue_says_what_to_do(self, bot):
        """Пустая очередь без подсказки — это тупик для человека с телефоном."""
        with db.write_transaction(bot["conn"]):
            bot["conn"].execute("UPDATE topics SET status = ?", (TopicStatus.USED,))

        text = bot["send"]("on_topics").answered[0]

        assert "Свободных тем нет" in text
        assert "списком" in text

    def test_a_long_queue_is_not_dumped_whole(self, bot):
        """Сотня тем одним сообщением не поместится и не нужна."""
        topics.add(bot["conn"], bot["project_id"], [f"Тема {n}" for n in range(40)])

        text = bot["send"]("on_topics").answered[0]

        assert "и ещё" in text
        assert text.count("•") <= topics.PREVIEW

    def test_a_stranger_sees_nothing(self, bot):
        assert "не для вас" in bot["send"]("on_topics", user=STRANGER).answered[0]

    def test_another_project_is_not_counted_in(self, bot):
        """Иначе чужая очередь выглядела бы как запас этой ниши."""
        from tests.conftest import insert_project, insert_topic

        other = insert_project(bot["conn"], "другой")
        for number in range(5):
            insert_topic(bot["conn"], other, f"Чужая {number}")

        text = bot["send"]("on_topics").answered[0]

        assert "свободных: 1" in text
        assert "Чужая" not in text

    def test_the_order_is_the_order_they_will_be_taken(self, bot):
        """Список нужен, чтобы понимать, о чём выйдет ближайший пост."""
        topics.add(bot["conn"], bot["project_id"], ["Первая", "Вторая", "Третья"])

        text = bot["send"]("on_topics").answered[0]

        shown = [line.strip("  • ") for line in text.splitlines() if line.strip().startswith("•")]
        assert shown.index("Первая") < shown.index("Вторая") < shown.index("Третья")


class TestAddingTopics:
    def test_a_plain_message_is_offered_as_topics(self, bot):
        message = bot["send"]("on_topics_offer", "Первая тема\nВторая тема")

        assert "Добавить 2 темы" in message.answered[0]
        assert message.markups[0] is not None, "не предложено подтверждение"

    def test_nothing_is_added_before_confirmation(self, bot):
        """Случайное сообщение не должно молча попасть в очередь и выйти постом."""
        bot["send"]("on_topics_offer", "Первая тема")

        assert "Первая тема" not in free_titles(bot["conn"], bot["project_id"])

    def test_confirming_adds_them(self, bot):
        bot["send"]("on_topics_offer", "Первая тема\nВторая тема")

        bot["press_topics"]("t:add:900")

        titles = free_titles(bot["conn"], bot["project_id"])
        assert "Первая тема" in titles
        assert "Вторая тема" in titles

    def test_refusing_adds_nothing(self, bot):
        bot["send"]("on_topics_offer", "Первая тема")

        bot["press_topics"]("t:no:900")

        assert "Первая тема" not in free_titles(bot["conn"], bot["project_id"])

    def test_repeats_are_skipped_and_reported(self, bot):
        topics.add(bot["conn"], bot["project_id"], ["Уже есть"])
        bot["send"]("on_topics_offer", "Уже есть\nНовая")

        query = bot["press_topics"]("t:add:900")

        assert "Добавлено тем: 1" in query.message.answered[0]
        assert "Пропущено (повторы и пустые): 1" in query.message.answered[0]

    def test_adding_clears_the_alert_about_an_empty_queue(self, bot):
        """Иначе тревога о кончившихся темах не прозвучит в следующий раз."""
        conn = bot["conn"]
        alerts.raise_once(
            conn, bot["providers"].notifier, chat_id=OWNER,
            name="no_topics", scope="demo", text="нечем публиковать",
        )
        bot["send"]("on_topics_offer", "Новая тема")

        bot["press_topics"]("t:add:900")

        assert not alerts.is_raised(conn, "no_topics", "demo")

    def test_a_stranger_cannot_fill_the_queue(self, bot):
        message = bot["send"]("on_topics_offer", "Чужая тема", user=STRANGER)

        assert "не для вас" in message.answered[0]
        assert "Чужая тема" not in free_titles(bot["conn"], bot["project_id"])

    def test_a_stranger_cannot_confirm_someone_elses_list(self, bot):
        bot["send"]("on_topics_offer", "Первая тема")

        query = bot["press_topics"]("t:add:900", user=STRANGER)

        assert "Первая тема" not in free_titles(bot["conn"], bot["project_id"])
        assert "не для вас" in query.said, "посторонний не получил ответа"

    def test_a_second_confirmation_does_not_double_them(self, bot):
        """Нажали дважды — темы не должны задвоиться.

        Список забывается сразу после применения: иначе повторное нажатие на то
        же сообщение добавит его ещё раз, а от дублей защищает только совпадение
        заголовков — стоит поправить одну букву, и в очереди две почти
        одинаковых темы.
        """
        bot["send"]("on_topics_offer", "Первая тема")
        bot["press_topics"]("t:add:900")
        assert bot["conn"].execute(
            "SELECT COUNT(*) FROM meta WHERE key LIKE 'pending_topics:%'"
        ).fetchone()[0] == 0, "список остался висеть после применения"

        bot["press_topics"]("t:add:900")

        assert free_titles(bot["conn"], bot["project_id"]).count("Первая тема") == 1


class TestPause:
    def test_pausing_hides_the_project_from_the_worker(self, bot):
        bot["send"]("on_pause")

        assert topics.is_paused(bot["conn"], "demo")

    def test_the_owner_is_told_what_stops(self, bot):
        text = bot["send"]("on_pause").answered[0]

        assert "публикаций не будет" in text
        assert "/resume" in text

    def test_resuming_brings_it_back(self, bot):
        bot["send"]("on_pause")

        bot["send"]("on_resume")

        assert not topics.is_paused(bot["conn"], "demo")

    def test_a_paused_project_is_skipped_by_the_tick(self, bot):
        from factory.core import machine

        bot["send"]("on_pause")

        assert machine.active_projects(bot["conn"]) == []

    def test_a_stranger_cannot_stop_the_factory(self, bot):
        bot["send"]("on_pause", user=STRANGER)

        assert not topics.is_paused(bot["conn"], "demo")


class TestRetryFromTheBot:
    """Сломанный пост чинится кнопкой, а не командой в терминале."""

    @pytest.fixture
    def broken(self, bot):
        with db.write_transaction(bot["conn"]):
            bot["conn"].execute(
                "UPDATE posts SET state = ?, retry_count = 5, last_error = 'сломалось' "
                "WHERE id = ?",
                (State.FAILED, bot["post_id"]),
            )
        return bot

    def test_it_returns_the_post_to_work(self, broken):
        conn, post_id = broken["conn"], broken["post_id"]

        assert apply(conn, post_id, Decision.RETRY, by=OWNER) is True

        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        assert row["state"] == State.QUEUED
        assert row["retry_count"] == 0
        assert row["last_error"] is None

    def test_it_does_not_count_as_an_approval(self, broken):
        """Иначе сломанный пост приближал бы публикацию без ревью."""
        from factory.core.decisions import approvals_in_a_row

        conn, post_id = broken["conn"], broken["post_id"]

        apply(conn, post_id, Decision.RETRY, by=OWNER)

        assert approvals_in_a_row(conn, broken["project_id"]) == 0

    def test_it_does_not_start_a_new_variant(self, broken):
        """Сломалось на последнем шаге — переделывать заново нечего."""
        conn, post_id = broken["conn"], broken["post_id"]
        before = conn.execute(
            "SELECT version FROM posts WHERE id = ?", (post_id,)
        ).fetchone()["version"]

        apply(conn, post_id, Decision.RETRY, by=OWNER)

        after = conn.execute(
            "SELECT version FROM posts WHERE id = ?", (post_id,)
        ).fetchone()["version"]
        assert after == before

    def test_a_healthy_post_is_not_touched(self, bot):
        conn, post_id = bot["conn"], bot["post_id"]

        assert apply(conn, post_id, Decision.RETRY, by=OWNER) is False

    def test_the_button_works_through_the_bot(self, broken):
        """Проверка всего пути, а не одной функции.

        Класс называется «чинится кнопкой» — значит проверять надо нажатие:
        права, применение, ответ. Без этого имя теста обещает больше, чем он
        делает.
        """
        query = FakeQuery(
            data=f"r:{broken['post_id']}:fix", from_user=FakeUser(OWNER)
        )
        asyncio.run(named(broken["dispatcher"], "callback", "on_decision").callback(query))

        state = broken["conn"].execute(
            "SELECT state FROM posts WHERE id = ?", (broken["post_id"],)
        ).fetchone()["state"]
        assert state == State.QUEUED
        assert "снова" in query.message.answered[-1]

    def test_a_stranger_cannot_press_it(self, broken):
        query = FakeQuery(
            data=f"r:{broken['post_id']}:fix", from_user=FakeUser(STRANGER)
        )
        asyncio.run(named(broken["dispatcher"], "callback", "on_decision").callback(query))

        state = broken["conn"].execute(
            "SELECT state FROM posts WHERE id = ?", (broken["post_id"],)
        ).fetchone()["state"]
        assert state == State.FAILED
        assert "не для вас" in query.said


class TestTheKeyAlwaysReachesTheRightHandler:
    """Ключ ВК не должен попадать ни в темы, ни в правку текста.

    Оба промаха неприятны одинаково: ключ не сохраняется, остаётся висеть в
    переписке, а владелец видит бессмысленный ответ вместо продолжения работы.
    """

    def test_a_bare_key_is_not_taken_for_topics(self, bot):
        """Голый ключ принимает разборщик — значит и маршрутизация обязана."""
        message = FakeMessage(text="vk1.a.qwertyuiop1234567890AB", from_user=FakeUser(OWNER))

        assert review_bot._looks_like_a_vk_key(message) is True
        assert review_bot._not_a_vk_key(message) is False

    def test_a_whole_address_is_recognised(self, bot):
        message = FakeMessage(
            text="https://oauth.vk.com/blank.html#access_token=vk1.a.qwertyuiop1234567890AB",
            from_user=FakeUser(OWNER),
        )

        assert review_bot._looks_like_a_vk_key(message) is True

    def test_a_topic_list_is_not_mistaken_for_a_key(self, bot):
        message = FakeMessage(text="Первая тема\nВторая тема", from_user=FakeUser(OWNER))

        assert review_bot._looks_like_a_vk_key(message) is False

    def test_the_key_handler_is_registered_before_the_others(self, bot):
        """aiogram берёт первый подошедший обработчик.

        Ключ, присланный ответом на сообщение с тревогой (самое естественное
        действие в телефоне — ответить туда, где ссылка), иначе уходит в правку
        текста.
        """
        names = [
            getattr(handler.callback, "__name__", "")
            for handler in bot["dispatcher"].message.handlers
        ]

        assert names.index("on_vk_token") < names.index("on_edit")
        assert names.index("on_vk_token") < names.index("on_topics_offer")


class TestRetryClearsTheAlarm:
    def test_a_fixed_post_can_alarm_again(self, bot):
        """Бот обещает написать ещё раз, если поломка повторится.

        Отметка о тревоге, оставшаяся после починки, делает это обещание
        невыполнимым: вторая поломка того же поста проходит молча.
        """
        conn, post_id = bot["conn"], bot["post_id"]
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET state = ?, last_error = 'сломалось' WHERE id = ?",
                (State.FAILED, post_id),
            )
        alerts.raise_once(
            conn, bot["providers"].notifier, chat_id=OWNER,
            name="failed", scope=f"demo:{post_id}", text="сломался",
        )

        apply(conn, post_id, Decision.RETRY, by=OWNER)

        assert not alerts.is_raised(conn, "failed", f"demo:{post_id}")

    def test_a_rollback_also_forgets_that_the_post_was_stuck(self, bot):
        """Откатили застрявший пост — о следующем застревании надо сказать заново."""
        conn, post_id = bot["conn"], bot["post_id"]
        with db.write_transaction(conn):
            conn.execute("UPDATE posts SET state = ? WHERE id = ?", (State.IN_REVIEW, post_id))
        alerts.raise_once(
            conn, bot["providers"].notifier, chat_id=OWNER,
            name="stuck", scope=f"demo:{post_id}", text="стоит сутки",
        )

        apply(conn, post_id, Decision.TEXT, by=OWNER)

        assert not alerts.is_raised(conn, "stuck", f"demo:{post_id}")


class TestTwoListsInARow:
    def test_each_button_adds_its_own_list(self, bot):
        """Метка едет в кнопке, а не хранится по человеку.

        Иначе второй список затирает первый: кнопка под первым сообщением
        добавляет чужие темы, а кнопка под вторым отвечает «не добавляю».
        """
        first = FakeMessage(text="Первая", from_user=FakeUser(OWNER), message_id=901)
        second = FakeMessage(text="Вторая", from_user=FakeUser(OWNER), message_id=902)
        asyncio.run(
            named(bot["dispatcher"], "message", "on_topics_offer").callback(first)
        )
        asyncio.run(
            named(bot["dispatcher"], "message", "on_topics_offer").callback(second)
        )

        bot["press_topics"]("t:add:901")
        bot["press_topics"]("t:add:902")

        titles = free_titles(bot["conn"], bot["project_id"])
        assert "Первая" in titles
        assert "Вторая" in titles

    def test_a_broken_button_is_refused(self, bot):
        query = bot["press_topics"]("t:add")

        assert "испорчена" in query.said


class TestTheCommandMenu:
    """Выпадашка по «/» — единственный способ узнать, что бот умеет.

    Без неё команды надо помнить наизусть, а владелец заходит в бота раз в день
    нажать одну кнопку.
    """

    def test_every_command_handler_is_in_the_menu(self, bot):
        listed = {name for name, _ in review_bot.COMMANDS}
        registered = set()
        for handler in bot["dispatcher"].message.handlers:
            for flt in handler.filters or ():
                commands = getattr(flt.callback, "commands", None)
                if commands:
                    registered.update(commands)

        assert registered - listed == set(), "команда есть, а в меню её нет"

    def test_the_menu_has_no_commands_that_do_not_exist(self, bot):
        names = [
            getattr(handler.callback, "__name__", "")
            for handler in bot["dispatcher"].message.handlers
        ]

        for name, _ in review_bot.COMMANDS:
            assert f"on_{name}" in names, f"в меню есть /{name}, а обработчика нет"

    def test_every_entry_explains_itself(self, bot):
        """Пустое описание в выпадашке бесполезно."""
        assert all(text.strip() for _, text in review_bot.COMMANDS)
