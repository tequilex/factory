"""Правка текста поста рукой владельца.

Модель написала складно, но одно слово не то. Без этой возможности выбор
нищий: публиковать как есть или переписывать весь пост заново, теряя всё
остальное, что было хорошо.

Главная тонкость — заголовок. Он печатается на обложке, поэтому смена
заголовка означает, что обложку надо собрать заново, а вместе с ней переслать
альбом. Смена только текста обложку не трогает, и слать картинки второй раз
незачем. Отсюда два разных возврата в цепочку.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.logging import get_logger
from factory.core.models import State
from factory.providers.base import TITLE_MAX_LENGTH

log = get_logger(__name__)


@dataclass(frozen=True)
class Edit:
    """Что владелец прислал и как это поняли."""

    title: str | None
    body: str
    cover_changes: bool


def parse(text: str) -> Edit | None:
    """Разобрать присланное на заголовок и текст.

    Владелец видит сообщение в виде «заголовок, пустая строка, текст» — и
    правит его целиком, копируя из чата. Поэтому тот же вид принимается
    обратно: короткая первая строка, отделённая пустой, считается заголовком.

    Если первая строка длинная или пустой строки нет — всё присланное это
    текст, а заголовок остаётся прежним. Догадываться дальше опасно: молча
    превратить первый абзац в заголовок обложки хуже, чем не тронуть его.
    """
    cleaned = text.strip()
    if not cleaned:
        return None

    head, separator, tail = cleaned.partition("\n\n")
    head = head.strip()
    tail = tail.strip()

    if separator and tail and "\n" not in head and len(head) <= TITLE_MAX_LENGTH:
        return Edit(title=head, body=tail, cover_changes=True)

    return Edit(title=None, body=cleaned, cover_changes=False)


def find_post_under(conn: sqlite3.Connection, message_id: int) -> int | None:
    """Пост, к сообщению которого владелец ответил."""
    row = conn.execute(
        "SELECT id FROM posts WHERE review_message_id = ? AND state = ?",
        (message_id, State.IN_REVIEW),
    ).fetchone()
    return row["id"] if row else None


def apply(conn: sqlite3.Connection, post_id: int, edit: Edit) -> bool:
    """Записать правку и вернуть пост на отправку. ``False`` — пост уже уехал."""
    stamp = to_iso(now_utc())

    with db.write_transaction(conn):
        row = conn.execute(
            "SELECT id FROM posts WHERE id = ? AND state = ?", (post_id, State.IN_REVIEW)
        ).fetchone()
        if row is None:
            return False

        if edit.cover_changes:
            # Заголовок печатается на обложке: её надо собрать заново, а значит
            # и картинки прислать снова. Метка сборки живёт в external_ref
            # обложки — шаг сборки смотрит именно на неё.
            conn.execute(
                "UPDATE assets SET external_ref = NULL WHERE post_id = ? AND kind = 'cover'",
                (post_id,),
            )
            target, album = State.IMAGES_READY, None
        else:
            # Картинки те же — второй раз альбом не шлём. Отметка о нём
            # сохраняется, и отправка ограничится новым текстом с кнопками.
            target, album = State.COMPOSED, "keep"

        conn.execute(
            "UPDATE posts SET title = COALESCE(?, title), body = ?, state = ?, "
            "retry_count = 0, last_error = NULL, next_attempt_at = NULL, "
            "review_message_id = NULL, "
            + (
                "review_album_at = review_album_at, "
                if album
                else "review_album_at = NULL, review_album_message_id = NULL, "
            )
            + "updated_at = ? WHERE id = ?",
            (edit.title, edit.body, target, stamp, post_id),
        )

    log.info(
        "текст поста поправлен владельцем",
        extra={"post_id": post_id, "f_title_changed": edit.cover_changes},
    )
    return True
