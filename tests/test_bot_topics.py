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
    reply_to_message: object = None
    answered: list[str] = field(default_factory=list)
    markups: list[object] = field(default_factory=list)

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answered.append(text)
        self.markups.append(reply_markup)


def routed_to(dispatcher, message) -> str | None:
    """Кто из обработчиков возьмёт сообщение первым.

    Повторяет выбор aiogram: обработчики перебираются в порядке регистрации,
    берётся первый, у которого прошли все фильтры.

    Проверять надо именно это, а не наличие обработчика в списке. Тест «функция
    зарегистрирована» переживает и подмену фильтра на «никогда», и вставку
    чужого обработчика перед нужным — обе поломки означают, что сообщение уедет
    не туда, а тест остаётся зелёным.
    """

    async def resolve():
        for handler in dispatcher.message.handlers:
            for flt in handler.filters or []:
                # bot=None нужен фильтру команд: он принимает его аргументом,
                # но обращается к нему только для разбора «/команда@имя_бота»,
                # а сюда такие сообщения не доходят.
                if not await flt.call(message, bot=None):
                    break
            else:
                return getattr(handler.callback, "__name__", None)
        return None

    return asyncio.run(resolve())


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
        assert "В запасе (1)" in text
        assert "тем всего: 1" in text

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

        assert "В запасе пусто" in text
        assert "списком" in text

    def test_a_long_queue_is_not_dumped_whole(self, bot):
        """Сотня тем одним сообщением не поместится и не нужна."""
        topics.add(bot["conn"], bot["project_id"], [f"Тема {n}" for n in range(40)])

        text = bot["send"]("on_topics").answered[0]

        assert "и ещё" in text
        listed = [line for line in text.splitlines() if line.strip()[:2].rstrip(".").isdigit()]
        assert len(listed) <= topics.PREVIEW

    def test_a_stranger_sees_nothing(self, bot):
        assert "не для вас" in bot["send"]("on_topics", user=STRANGER).answered[0]

    def test_another_project_is_not_counted_in(self, bot):
        """Иначе чужая очередь выглядела бы как запас этой ниши."""
        from tests.conftest import insert_project, insert_topic

        other = insert_project(bot["conn"], "другой")
        for number in range(5):
            insert_topic(bot["conn"], other, f"Чужая {number}")

        text = bot["send"]("on_topics").answered[0]

        assert "В запасе (1)" in text
        assert "Чужая" not in text

    def test_the_order_is_the_order_they_will_be_taken(self, bot):
        """Список нужен, чтобы понимать, о чём выйдет ближайший пост."""
        topics.add(bot["conn"], bot["project_id"], ["Первая", "Вторая", "Третья"])

        text = bot["send"]("on_topics").answered[0]

        shown = [
            line.split(". ", 1)[1]
            for line in text.splitlines()
            if line.strip()[:2].rstrip(".").isdigit() and ". " in line
        ]
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
    """Ключ и код ВК не должны попадать ни в темы, ни в правку текста.

    Промахи неприятны одинаково: ключ не сохраняется, остаётся висеть в
    переписке, а владелец видит бессмысленный ответ вместо продолжения работы.
    """

    def test_a_bare_key_is_not_taken_for_topics(self, bot):
        """Голый ключ принимает разборщик — значит и маршрутизация обязана."""
        message = FakeMessage(text="vk1.a.qwertyuiop1234567890AB", from_user=FakeUser(OWNER))

        assert review_bot._looks_like_a_vk_key(message) is True
        assert review_bot._not_a_vk_secret(message) is False

    def test_a_code_is_not_taken_for_topics(self, bot):
        """Поймано живьём: код уехал в темы, и бот предложил его добавить.

        Ключ при этом остался протухшим, публикация стояла, а на вид всё
        работало — бот ведь ответил. Обработчик кода существовал, но не был
        зарегистрирован ни на один фильтр.
        """
        message = FakeMessage(
            text="https://oauth.vk.ru/blank.html#code=4b59a4fb40ab1805e3",
            from_user=FakeUser(OWNER),
        )

        assert review_bot._looks_like_a_vk_code(message) is True
        assert review_bot._not_a_vk_secret(message) is False

    def test_a_bare_code_is_recognised_too(self, bot):
        """С телефона копируют по-разному: и весь адрес, и один код."""
        message = FakeMessage(text="4b59a4fb40ab1805e3", from_user=FakeUser(OWNER))

        assert review_bot._looks_like_a_vk_code(message) is True

    def test_a_topic_list_is_not_mistaken_for_a_code(self, bot):
        message = FakeMessage(text="Первая тема\nВторая тема", from_user=FakeUser(OWNER))

        assert review_bot._looks_like_a_vk_code(message) is False
        assert review_bot._not_a_vk_secret(message) is True

    def test_a_code_reaches_the_code_handler(self, bot):
        """Написанный, но не подключённый обработчик — то же, что его отсутствие.

        Проверяется маршрутизация целиком: какой обработчик реально возьмёт
        сообщение. Именно так эта поломка и выглядела снаружи — обработчик в
        коде был, а код уезжал в темы.
        """
        message = FakeMessage(
            text="https://oauth.vk.ru/blank.html#code=4b59a4fb40ab1805e3",
            from_user=FakeUser(OWNER),
        )

        assert routed_to(bot["dispatcher"], message) == "on_vk_code"

    def test_a_code_sent_as_a_reply_still_reaches_the_code_handler(self, bot):
        """Самое естественное действие в телефоне — ответить туда, где ссылка.

        Ответ на сообщение по умолчанию считается правкой текста поста. Код,
        присланный ответом, обязан всё равно уйти в обновление ключа.
        """
        message = FakeMessage(
            text="https://oauth.vk.ru/blank.html#code=4b59a4fb40ab1805e3",
            from_user=FakeUser(OWNER),
            reply_to_message=object(),
        )

        assert routed_to(bot["dispatcher"], message) == "on_vk_code"

    def test_a_key_reaches_the_key_handler(self, bot):
        message = FakeMessage(
            text="vk1.a.qwertyuiop1234567890AB", from_user=FakeUser(OWNER)
        )

        assert routed_to(bot["dispatcher"], message) == "on_vk_token"

    def test_a_topic_list_reaches_the_topics_handler(self, bot):
        message = FakeMessage(text="Первая тема\nВторая тема", from_user=FakeUser(OWNER))

        assert routed_to(bot["dispatcher"], message) == "on_topics_offer"

    def test_a_whole_address_is_recognised(self, bot):
        message = FakeMessage(
            text="https://oauth.vk.com/blank.html#access_token=vk1.a.qwertyuiop1234567890AB",
            from_user=FakeUser(OWNER),
        )

        assert review_bot._looks_like_a_vk_key(message) is True

    def test_a_topic_list_is_not_mistaken_for_a_key(self, bot):
        message = FakeMessage(text="Первая тема\nВторая тема", from_user=FakeUser(OWNER))

        assert review_bot._looks_like_a_vk_key(message) is False

    def test_a_key_sent_as_a_reply_still_reaches_the_key_handler(self, bot):
        """Ключ, присланный ответом на сообщение с тревогой, — обычное дело.

        Ответить туда, где ссылка, — самое естественное действие в телефоне. По
        умолчанию ответ считается правкой текста поста, и без правильного
        порядка обработчиков ключ уходил бы туда.

        Проверяется маршрутизацией, а не порядком имён в списке: проверка по
        именам переживает вставку чужого обработчика перед нужным.
        """
        message = FakeMessage(
            text="vk1.a.qwertyuiop1234567890AB",
            from_user=FakeUser(OWNER),
            reply_to_message=object(),
        )

        assert routed_to(bot["dispatcher"], message) == "on_vk_token"


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


class TestSchedule:
    """Расписание переключается из телефона, а не переменной окружения.

    Переменную задаёт тот, кто запускает процесс; владелец до неё не дотянется,
    а решать «выходит сразу или ждёт вечера» приходится ему.
    """

    def test_by_default_posts_wait_for_a_slot(self, bot):
        assert not topics.schedule_is_off(bot["conn"], "demo")

    def test_it_can_be_switched_off(self, bot):
        bot["send"]("on_schedule_off")

        assert topics.schedule_is_off(bot["conn"], "demo")

    def test_and_back_on(self, bot):
        bot["send"]("on_schedule_off")

        bot["send"]("on_schedule_on")

        assert not topics.schedule_is_off(bot["conn"], "demo")

    def test_the_state_is_shown_with_the_slots(self, bot):
        text = bot["send"]("on_schedule").answered[0]

        assert "слота" in text or "слоты" in text.lower()

    def test_switching_off_warns_the_limit_still_applies(self, bot):
        """Лимит про количество, а не про время — иначе владелец решит, что сломалось."""
        text = bot["send"]("on_schedule_off").answered[0]

        assert "лимит" in text

    def test_a_stranger_cannot_switch_it(self, bot):
        bot["send"]("on_schedule_off", user=STRANGER)

        assert not topics.schedule_is_off(bot["conn"], "demo")

    def test_the_environment_switch_still_wins(self, bot, monkeypatch):
        """Глобальный рубильник задаёт тот, кто запускает процесс.

        Молча отменять его решение из переписки нельзя: человек за терминалом
        включил его осознанно и не увидит, что настройку перебили.
        """
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        bot["send"]("on_schedule_on")

        assert topics.schedule_is_off(bot["conn"], "demo")

    def test_each_project_switches_on_its_own(self, bot):
        from tests.conftest import insert_project

        insert_project(bot["conn"], "другой")
        bot["send"]("on_schedule_off")

        assert topics.schedule_is_off(bot["conn"], "demo")
        assert not topics.schedule_is_off(bot["conn"], "другой")


class TestTopicListsTellTheWholeStory:
    """Числа отвечают на «сколько», а спрашивают обычно «а что именно».

    Без списков нельзя ни понять, чем кормить систему дальше, ни вспомнить,
    о чём уже выходило, — и темы начинают повторяться.
    """

    def in_work(self, bot, title, state):
        from tests.conftest import insert_post, insert_topic

        conn = bot["conn"]
        topic = insert_topic(conn, bot["project_id"], title)
        post = insert_post(conn, bot["project_id"], topic, idem_key=f"demo:{topic}:0")
        with db.write_transaction(conn):
            conn.execute("UPDATE topics SET status = ? WHERE id = ?", (TopicStatus.TAKEN, topic))
            conn.execute("UPDATE posts SET state = ? WHERE id = ?", (state, post))
        return post

    def finished(self, bot, title, external_id=None):
        from tests.conftest import insert_post, insert_topic

        conn = bot["conn"]
        topic = insert_topic(conn, bot["project_id"], title)
        post = insert_post(conn, bot["project_id"], topic, idem_key=f"demo:{topic}:0")
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE topics SET status = ?, used_at = ? WHERE id = ?",
                (TopicStatus.USED, "2026-08-25T12:00:00Z", topic),
            )
            conn.execute(
                "UPDATE posts SET state = ?, external_id = ? WHERE id = ?",
                (State.PUBLISHED if external_id else State.REJECTED, external_id, post),
            )

    def test_a_topic_in_work_shows_which_step_it_is_on(self, bot):
        """«В работе» одинаково у поста, ждущего решения, и у сломавшегося час назад."""
        self.in_work(bot, "Ждёт меня", State.IN_REVIEW)

        text = bot["send"]("on_topics").answered[0]

        assert "Ждёт меня — ждёт вашего решения" in text

    def test_every_step_has_human_words(self, bot):
        for state, word in [
            (State.QUEUED, "пишется текст"),
            (State.APPROVED, "одобрен, ждёт слота"),
            (State.FAILED, "сломался"),
        ]:
            assert topics.STATE_WORDS[state] == word

    def test_a_published_topic_comes_with_a_link(self, bot):
        """Чтобы посмотреть, что вышло, не листая группу."""
        self.finished(bot, "Уже вышла", external_id="-111222333_10")

        text = bot["send"]("on_topics").answered[0]

        assert "Уже вышла — https://vk.com/wall-111222333_10" in text

    def test_a_topic_closed_without_a_post_says_so(self, bot):
        self.finished(bot, "Закрыта впустую")

        text = bot["send"]("on_topics").answered[0]

        assert "Закрыта впустую — закрыта без поста" in text

    def test_the_freshest_finished_topics_come_first(self, bot):
        """Спрашивают «что сделано» про последнее, а не про самое первое."""
        conn = bot["conn"]
        self.finished(bot, "Позавчерашняя", external_id="-1_1")
        self.finished(bot, "Сегодняшняя", external_id="-1_2")
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE topics SET used_at = ? WHERE title = ?",
                ("2026-08-20T10:00:00Z", "Позавчерашняя"),
            )
            conn.execute(
                "UPDATE topics SET used_at = ? WHERE title = ?",
                ("2026-08-25T10:00:00Z", "Сегодняшняя"),
            )

        text = bot["send"]("on_topics").answered[0]

        assert text.index("Сегодняшняя") < text.index("Позавчерашняя")

    def test_the_three_lists_do_not_mix(self, bot):
        self.in_work(bot, "Делается", State.QUEUED)
        self.finished(bot, "Сделана", external_id="-1_3")

        text = bot["send"]("on_topics").answered[0]

        reserve = text.index("В запасе")
        working = text.index("В работе")
        finished = text.index("Отработано")
        assert reserve < working < finished
        assert reserve < text.index("Как выбрать шины") < working
        assert working < text.index("Делается") < finished
        assert finished < text.index("Сделана")

    def test_long_lists_say_how_many_are_hidden(self, bot):
        for number in range(topics.PREVIEW + 5):
            self.finished(bot, f"Вышла {number}", external_id=f"-1_{number}")

        text = bot["send"]("on_topics").answered[0]

        assert "…и ещё 5" in text

    def test_another_projects_topics_never_appear(self, bot):
        """Во всех трёх списках, а не только в запасе.

        Чужая отработанная тема в разделе «сделано» — это чужой пост, выданный
        за свой, и повод не написать о том, о чём на самом деле не писали.
        """
        from tests.conftest import insert_post, insert_project, insert_topic

        conn = bot["conn"]
        other = insert_project(conn, "чужой")
        insert_topic(conn, other, "Чужая в запасе")

        for title, status, state in (
            ("Чужая в работе", TopicStatus.TAKEN, State.IN_REVIEW),
            ("Чужая сделана", TopicStatus.USED, State.PUBLISHED),
        ):
            topic = insert_topic(conn, other, title)
            post = insert_post(conn, other, topic, idem_key=f"чужой:{topic}:0")
            with db.write_transaction(conn):
                conn.execute("UPDATE topics SET status = ? WHERE id = ?", (status, topic))
                conn.execute(
                    "UPDATE posts SET state = ?, external_id = ? WHERE id = ?",
                    (state, "-9_9", post),
                )

        text = bot["send"]("on_topics").answered[0]

        assert "Чужая" not in text


class TestTrashAsksWhatExactly:
    """«В мусор» звучит как «выбросить всё», а выбрасывался только пост.

    Тема возвращалась в очередь на своё старое — обычно первое — место, и по
    ней тут же писался такой же пост. Владелец при этом ничего не выбирал.
    """

    @pytest.fixture
    def ready(self, bot):
        with db.write_transaction(bot["conn"]):
            bot["conn"].execute(
                "UPDATE posts SET state = ? WHERE id = ?", (State.IN_REVIEW, bot["post_id"])
            )
            bot["conn"].execute(
                "UPDATE topics SET status = ? WHERE id = ?",
                (TopicStatus.TAKEN, bot["topic_id"]),
            )

        def press(action: str):
            query = FakeQuery(
                data=f"r:{bot['post_id']}:{action}:1", from_user=FakeUser(OWNER)
            )
            asyncio.run(named(bot["dispatcher"], "callback", "on_decision").callback(query))
            return query

        bot["decide"] = press
        return bot

    def state_of(self, bot):
        return bot["conn"].execute(
            "SELECT state FROM posts WHERE id = ?", (bot["post_id"],)
        ).fetchone()["state"]

    def topic_status(self, bot):
        return bot["conn"].execute(
            "SELECT status FROM topics WHERE id = ?", (bot["topic_id"],)
        ).fetchone()["status"]

    def test_the_first_press_throws_nothing_away(self, ready):
        query = ready["decide"]("ask")

        assert self.state_of(ready) == State.IN_REVIEW
        buttons = [b for row in query.message.last_markup.inline_keyboard for b in row]
        assert len(buttons) == 3

    def test_going_back_returns_the_usual_buttons(self, ready):
        ready["decide"]("ask")

        query = ready["decide"]("keep")

        assert self.state_of(ready) == State.IN_REVIEW
        buttons = [b for row in query.message.last_markup.inline_keyboard for b in row]
        assert len(buttons) == 5

    def test_only_the_post_returns_the_topic_to_the_queue(self, ready):
        ready["decide"]("del")

        assert self.state_of(ready) == State.REJECTED
        assert self.topic_status(ready) == TopicStatus.FREE

    def test_the_returned_topic_goes_to_the_end_of_the_queue(self, ready):
        """Иначе выбросил пост — и тут же получил такой же по той же теме."""
        conn = ready["conn"]
        ready["decide"]("del")

        row = conn.execute(
            "SELECT requeued_at FROM topics WHERE id = ?", (ready["topic_id"],)
        ).fetchone()
        assert row["requeued_at"] is not None

    def test_a_requeued_topic_is_taken_after_untouched_ones(self, ready):
        from factory.core import machine
        from tests.conftest import insert_topic

        conn = ready["conn"]
        ready["decide"]("del")
        fresh = insert_topic(conn, ready["project_id"], "Свежая тема")

        with db.write_transaction(conn):
            taken = machine._claim_locked(conn, ready["project_id"])

        assert taken == fresh, "вернувшаяся тема опять встала первой"

    def test_post_and_topic_closes_the_topic(self, ready):
        ready["decide"]("delt")

        assert self.state_of(ready) == State.REJECTED
        assert self.topic_status(ready) == TopicStatus.USED

    def test_both_ways_are_recorded_as_a_rejection(self, ready):
        ready["decide"]("delt")

        row = ready["conn"].execute(
            "SELECT reason FROM rejections WHERE post_id = ?", (ready["post_id"],)
        ).fetchone()
        assert row["reason"] == "trash"

    def test_the_owner_is_told_which_one_happened(self, ready):
        query = ready["decide"]("del")
        assert "в конец" in query.message.answered[-1]

        with db.write_transaction(ready["conn"]):
            ready["conn"].execute(
                "UPDATE posts SET state = ? WHERE id = ?", (State.IN_REVIEW, ready["post_id"])
            )
        query = ready["decide"]("delt")
        assert "тема закрыта" in query.message.answered[-1]

    def test_a_stranger_cannot_even_open_the_dialog(self, ready):
        query = FakeQuery(
            data=f"r:{ready['post_id']}:ask:1", from_user=FakeUser(STRANGER)
        )
        asyncio.run(named(ready["dispatcher"], "callback", "on_decision").callback(query))

        assert "не для вас" in query.said
        assert query.message.last_markup is None


class TestTheBotGoesThroughTheProxy:
    """aiogram ходит своим клиентом и core/http.py не использует.

    Из-за этого настройка telegram.proxy_env его не касалась вовсе: на малине
    воркер отправлял посты через прокси, а бот молчал с таймаутом, потому что
    шёл напрямую в заблокированный Telegram.
    """

    def test_a_proxy_is_used_when_configured(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_PROXY", "socks5://proxy:7890")

        assert review_bot._session() is not None

    def test_without_a_proxy_the_connection_is_direct(self, monkeypatch):
        for name in ("TELEGRAM_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            monkeypatch.delenv(name, raising=False)

        assert review_bot._session() is None

    def test_it_asks_the_same_resolver_as_the_worker(self, monkeypatch):
        """Две настройки прокси для одного Telegram разойдутся при первой правке."""
        from factory.core import http

        monkeypatch.setenv("TELEGRAM_PROXY", "socks5://proxy:7890")
        seen = []
        monkeypatch.setattr(
            http, "proxy_for", lambda provider, **kw: seen.append(provider) or None
        )

        review_bot._session()

        assert seen == ["telegram"]
