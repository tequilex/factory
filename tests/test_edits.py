"""Правка текста поста рукой владельца.

Смысл возможности — не переписывать весь пост из-за одного слова. Отсюда две
вещи, которые здесь проверяются жёстче остального: что правка не теряет ничего
лишнего и что обложка пересобирается ровно тогда, когда поменялся заголовок.
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from factory.core import db, edits
from factory.core.config import TelegramCfg
from factory.core.models import State
from factory.providers.base import TITLE_MAX_LENGTH

OWNER = 123456789


def post_row(conn, post_id):
    return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


def cover(conn, post_id):
    return conn.execute(
        "SELECT * FROM assets WHERE post_id = ? AND kind = 'cover'", (post_id,)
    ).fetchone()


class TestParse:
    """Владелец правит то, что видит: заголовок, пустая строка, текст."""

    def test_a_title_and_a_body(self):
        edit = edits.parse("Новый заголовок\n\nНовое тело поста.")

        assert edit.title == "Новый заголовок"
        assert edit.body == "Новое тело поста."
        assert edit.cover_changes is True

    def test_plain_text_leaves_the_title_alone(self):
        """Догадка «первый абзац — это заголовок» испортила бы обложку."""
        edit = edits.parse("Просто исправленный текст без пустых строк.")

        assert edit.title is None
        assert edit.cover_changes is False
        assert edit.body == "Просто исправленный текст без пустых строк."

    def test_a_long_first_line_is_not_a_title(self):
        """Заголовок не влезет в обложку — значит это абзац, а не заголовок."""
        long_line = "о" * (TITLE_MAX_LENGTH + 1)

        edit = edits.parse(f"{long_line}\n\nвторой абзац")

        assert edit.title is None
        assert edit.body.startswith(long_line)
        assert "второй абзац" in edit.body

    def test_a_multiline_head_is_not_a_title(self):
        edit = edits.parse("первая строка\nвторая строка\n\nтело")

        assert edit.title is None

    def test_the_whole_body_survives_several_paragraphs(self):
        edit = edits.parse("Заголовок\n\nПервый абзац.\n\nВторой абзац.")

        assert edit.title == "Заголовок"
        assert "Первый абзац." in edit.body
        assert "Второй абзац." in edit.body

    @pytest.mark.parametrize("text", ["", "   ", "\n\n"])
    def test_nothing_to_apply(self, text):
        assert edits.parse(text) is None


@pytest.fixture
def in_review(pipeline):
    pipeline["advance_through"](
        State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
        State.PROMPTS_READY, State.IMAGES_READY, State.COMPOSED,
    )
    pipeline["context"](State.IN_REVIEW)
    with db.write_transaction(pipeline["conn"]):
        pipeline["conn"].execute(
            "UPDATE posts SET review_message_id = 500, review_album_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00Z", pipeline["post_id"]),
        )
    return pipeline


class TestApplyBodyOnly:
    def test_the_body_is_replaced(self, in_review):
        conn, post_id = in_review["conn"], in_review["post_id"]

        assert edits.apply(conn, post_id, edits.parse("Совсем другой текст."))

        assert post_row(conn, post_id)["body"] == "Совсем другой текст."

    def test_the_title_is_kept(self, in_review):
        conn, post_id = in_review["conn"], in_review["post_id"]
        title = post_row(conn, post_id)["title"]

        edits.apply(conn, post_id, edits.parse("Другой текст."))

        assert post_row(conn, post_id)["title"] == title

    def test_the_cover_is_not_redrawn(self, in_review):
        """Заголовок тот же — перерисовывать обложку не за что."""
        conn, post_id = in_review["conn"], in_review["post_id"]
        mark = cover(conn, post_id)["external_ref"]
        assert mark, "обложка не помечена собранной — тест не о том"

        edits.apply(conn, post_id, edits.parse("Другой текст."))

        assert cover(conn, post_id)["external_ref"] == mark
        assert post_row(conn, post_id)["state"] == State.COMPOSED

    def test_the_album_is_not_sent_again(self, in_review):
        """Картинки не менялись: слать их второй раз — спам."""
        conn, post_id = in_review["conn"], in_review["post_id"]

        edits.apply(conn, post_id, edits.parse("Другой текст."))

        assert post_row(conn, post_id)["review_album_at"] is not None

    def test_the_post_comes_back_for_a_new_look(self, in_review):
        conn, post_id = in_review["conn"], in_review["post_id"]

        edits.apply(conn, post_id, edits.parse("Другой текст."))

        row = post_row(conn, post_id)
        assert row["review_message_id"] is None, "старое сообщение осталось активным"
        assert row["next_attempt_at"] is None


class TestApplyWithTitle:
    def test_the_title_is_replaced(self, in_review):
        conn, post_id = in_review["conn"], in_review["post_id"]

        edits.apply(conn, post_id, edits.parse("Свежий заголовок\n\nТекст."))

        assert post_row(conn, post_id)["title"] == "Свежий заголовок"

    def test_the_cover_is_redrawn(self, in_review):
        """Заголовок печатается на обложке — старая обложка теперь врёт."""
        conn, post_id = in_review["conn"], in_review["post_id"]

        edits.apply(conn, post_id, edits.parse("Свежий заголовок\n\nТекст."))

        assert cover(conn, post_id)["external_ref"] is None
        assert post_row(conn, post_id)["state"] == State.IMAGES_READY

    def test_the_album_is_sent_again(self, in_review):
        """Обложка изменилась — картинки надо показать заново."""
        conn, post_id = in_review["conn"], in_review["post_id"]

        edits.apply(conn, post_id, edits.parse("Свежий заголовок\n\nТекст."))

        assert post_row(conn, post_id)["review_album_at"] is None

    def test_the_cover_really_gets_the_new_title(self, in_review):
        """Проверка сторожа: шаг сборки обязан взяться за работу заново."""
        conn, post_id = in_review["conn"], in_review["post_id"]
        edits.apply(conn, post_id, edits.parse("Свежий заголовок\n\nТекст."))

        result, _ = in_review["run"](State.IMAGES_READY)

        assert result.advanced
        assert cover(conn, post_id)["external_ref"], "обложка не пересобрана"


class TestGuards:
    def test_a_post_that_already_left_review_is_refused(self, in_review):
        conn, post_id = in_review["conn"], in_review["post_id"]
        with db.write_transaction(conn):
            conn.execute("UPDATE posts SET state = ? WHERE id = ?", (State.APPROVED, post_id))

        assert edits.apply(conn, post_id, edits.parse("Поздно.")) is False
        assert post_row(conn, post_id)["body"] != "Поздно."

    def test_the_post_is_found_by_the_message_replied_to(self, in_review):
        assert edits.find_post_under(in_review["conn"], 500) == in_review["post_id"]

    def test_an_unknown_message_finds_nothing(self, in_review):
        assert edits.find_post_under(in_review["conn"], 999) is None

    def test_a_decided_post_is_not_found(self, in_review):
        """Ответ на старое сообщение не должен править уехавший пост."""
        conn = in_review["conn"]
        with db.write_transaction(conn):
            conn.execute("UPDATE posts SET state = ?", (State.PUBLISHED,))

        assert edits.find_post_under(conn, 500) is None


@dataclass
class FakeMessage:
    text: str
    reply_to_message: object = None
    from_user: object = None
    answered: list[str] = field(default_factory=list)

    async def answer(self, text: str) -> None:
        self.answered.append(text)


@dataclass
class FakeUser:
    id: int


@dataclass
class FakeReplied:
    message_id: int


class TestThroughTheBot:
    @pytest.fixture
    def env(self, in_review):
        from factory.bot import review_bot

        project = in_review["project"]
        asking = project.model_copy(
            update={
                "review": project.review.model_copy(update={"mode": "telegram"}),
                "telegram": TelegramCfg(provider="stub", chat_id=OWNER, reviewers=[OWNER]),
            }
        )

        def send(text, *, reply_to=500, user=OWNER):
            message = FakeMessage(
                text=text, reply_to_message=FakeReplied(reply_to), from_user=FakeUser(user)
            )
            asyncio.run(review_bot._accept_edit(in_review["conn"], {"demo": asking}, message))
            return message

        in_review["send"] = send
        return in_review

    def test_the_owner_is_told_what_was_understood(self, env):
        message = env["send"]("Заголовок покороче\n\nТекст поста.")

        assert "Заголовок покороче" in message.answered[0]

    def test_a_body_only_edit_says_the_title_is_kept(self, env):
        """Иначе владелец не поймёт, почему заголовок не изменился."""
        message = env["send"]("Просто текст.")

        assert "заголовок" in message.answered[0].lower()

    def test_a_stranger_cannot_rewrite_the_post(self, env):
        conn, post_id = env["conn"], env["post_id"]
        body = post_row(conn, post_id)["body"]

        message = env["send"]("Мой текст.", user=999)

        assert post_row(conn, post_id)["body"] == body
        assert "не для вас" in message.answered[0]

    def test_a_reply_to_something_else_is_explained(self, env):
        message = env["send"]("Текст.", reply_to=12345)

        assert "к какому посту" in message.answered[0]
