"""Applying the owner's decision to a post.

Separate from the bot on purpose: this is the part that must be right, and it is
testable without Telegram at all. The bot is a thin layer that turns a button
press into a call here.

Every rollback must **erase data, not just change state**. Steps skip their work
when the data already exists — that is what keeps a restart from paying twice for
the same text. The same guard makes a naive rollback a lie: the post returns to
``queued``, the text step sees a title and a body, skips, and the owner gets back
the exact post they rejected. So each rollback below clears precisely what the
step it returns to checks:

* ``text`` checks ``title and body`` — and ``factcheck`` checks the verdict, so a
  stale verdict would silently skip the check on a brand new text;
* ``prompts`` counts rows in ``assets``;
* ``images`` checks ``local_path``, and the same image comes back if ``seed``
  stays;
* ``compose`` checks a mark in ``external_ref`` — miss it and the old cover
  survives a full image regeneration.

Every decision is guarded by ``WHERE state = 'in_review'``. Pressing a button
twice, or pressing one on an old message, updates zero rows and changes nothing.
"""

from __future__ import annotations

import json
import random
import sqlite3
from enum import StrEnum

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.logging import get_logger
from factory.core.models import State, TopicStatus

log = get_logger(__name__)

# Seeds are drawn from the same range the prompts step uses.
SEED_MAX = 2**31 - 1


class Decision(StrEnum):
    """Что владелец нажал. Значения уходят в callback_data — менять нельзя."""

    APPROVE = "ok"
    IMAGES = "img"
    SCENES = "scn"
    TEXT = "txt"
    TRASH = "del"


#: Куда откатывается пост и по какой причине это записывается в ``rejections``.
#: ``None`` в причине — решение не является отказом.
TARGET_STATE: dict[Decision, State] = {
    Decision.APPROVE: State.APPROVED,
    Decision.IMAGES: State.PROMPTS_READY,
    Decision.SCENES: State.FACTCHECKED,
    Decision.TEXT: State.QUEUED,
    Decision.TRASH: State.REJECTED,
}

REJECTION_REASON: dict[Decision, str | None] = {
    Decision.APPROVE: None,
    Decision.IMAGES: "images",
    Decision.SCENES: "scenes",
    Decision.TEXT: "text",
    Decision.TRASH: "trash",
}

LABEL: dict[Decision, str] = {
    Decision.APPROVE: "Опубликовать",
    Decision.IMAGES: "Картинки заново",
    Decision.SCENES: "Другие сцены",
    Decision.TEXT: "Текст заново",
    Decision.TRASH: "В мусор",
}


def _snapshot(conn: sqlite3.Connection, post_id: int) -> str:
    """Что было в посте на момент отказа. Будущий обучающий набор."""
    row = conn.execute(
        "SELECT title, body, question, factcheck_verdict, factcheck_notes "
        "FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    prompts = conn.execute(
        "SELECT kind, position, prompt, seed FROM assets WHERE post_id = ? ORDER BY position",
        (post_id,),
    ).fetchall()
    return json.dumps(
        {
            "post": dict(row) if row else {},
            "assets": [dict(item) for item in prompts],
        },
        ensure_ascii=False,
    )


def _clear_for(conn: sqlite3.Connection, decision: Decision, post_id: int) -> None:
    """Стереть ровно то, на что смотрит шаг, куда пост возвращается."""
    if decision is Decision.TEXT:
        conn.execute(
            "UPDATE posts SET title = NULL, body = NULL, question = NULL, "
            "factcheck_verdict = NULL, factcheck_notes = NULL WHERE id = ?",
            (post_id,),
        )
        # Промпты и картинки сочинялись по старому тексту и к новому не подходят.
        conn.execute("DELETE FROM assets WHERE post_id = ?", (post_id,))

    elif decision is Decision.SCENES:
        conn.execute("DELETE FROM assets WHERE post_id = ?", (post_id,))

    elif decision is Decision.IMAGES:
        # Промпты остаются: претензия к рисунку, а не к замыслу. Меняется seed —
        # при том же значении модель вернёт ровно ту же картинку.
        for asset in conn.execute(
            "SELECT id FROM assets WHERE post_id = ?", (post_id,)
        ).fetchall():
            conn.execute(
                "UPDATE assets SET local_path = NULL, external_ref = NULL, seed = ? WHERE id = ?",
                (random.randint(1, SEED_MAX), asset["id"]),
            )


def apply(
    conn: sqlite3.Connection,
    post_id: int,
    decision: Decision,
    *,
    by: int | None = None,
) -> bool:
    """Применить решение. ``False`` — пост уже не в ревью, ничего не изменено.

    Всё одной транзакцией: снимок, очистка, смена состояния, возврат темы.
    Половина применённого решения хуже неприменённого — пост с пустым текстом
    в состоянии ``in_review`` не двинется ни в одну сторону.
    """
    target = TARGET_STATE[decision]
    reason = REJECTION_REASON[decision]
    stamp = to_iso(now_utc())

    with db.write_transaction(conn):
        row = conn.execute(
            "SELECT id, topic_id FROM posts WHERE id = ? AND state = ?",
            (post_id, State.IN_REVIEW),
        ).fetchone()
        if row is None:
            return False

        if reason is not None:
            conn.execute(
                "INSERT INTO rejections (post_id, reason, snapshot, created_at) "
                "VALUES (?, ?, ?, ?)",
                (post_id, reason, _snapshot(conn, post_id), stamp),
            )

        _clear_for(conn, decision, post_id)

        if decision is Decision.TRASH:
            # Тема не потрачена: по ней будет новый пост с другим idem_key.
            conn.execute(
                "UPDATE topics SET status = ? WHERE id = ?",
                (TopicStatus.FREE, row["topic_id"]),
            )

        # Сбрасываются и счётчик попыток, и время следующей: пост возвращается
        # в работу немедленно и с чистым бюджетом, а не с наследством от того,
        # что владелец забраковал. Сообщение с кнопками больше не актуально.
        conn.execute(
            "UPDATE posts SET state = ?, retry_count = 0, last_error = NULL, "
            "next_attempt_at = NULL, review_message_id = NULL, "
            "decided_at = ?, decided_by = ?, updated_at = ? WHERE id = ?",
            (target, stamp, by, stamp, post_id),
        )

    log.info(
        "решение владельца применено",
        extra={"post_id": post_id, "decision": str(decision), "state": str(target), "by": by},
    )
    return True


def approvals_in_a_row(conn: sqlite3.Connection, project_id: int) -> int:
    """Сколько постов одобрено подряд без единой правки.

    Считается запросом, а не счётчиком в базе: счётчик пришлось бы наращивать и
    обнулять в трёх местах, и он разошёлся бы с реальностью при первом же
    откате. На данных ответ точен всегда.

    Сравнивать метки времени отказа и одобрения нельзя: они пишутся с точностью
    до секунды, и два решения, принятые подряд, оказываются одновременными.
    Поэтому идём по постам от последнего решённого и останавливаемся на первом,
    которому потребовалась правка. Пост, который откатывали и потом одобрили,
    правку потребовал — он обрывает счёт, а не продолжает его.
    """
    rows = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM rejections r WHERE r.post_id = p.id) AS was_fixed "
        "FROM posts p WHERE p.project_id = ? AND p.decided_at IS NOT NULL "
        "ORDER BY p.decided_at DESC, p.id DESC",
        (project_id,),
    ).fetchall()

    streak = 0
    for row in rows:
        if row["was_fixed"]:
            break
        streak += 1
    return streak
