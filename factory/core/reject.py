"""Rejecting a post and putting its topic back in the queue.

Every rejection is recorded with a snapshot of what was rejected. That table is
the point: over months it becomes a dataset of "what this owner does not want",
which is worth far more than the disk space it costs.

The topic goes back to ``free`` and will be picked up again — with a fresh post
whose ``idem_key`` carries the next attempt number.
"""

from __future__ import annotations

import json
import sqlite3

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.errors import FactoryError
from factory.core.models import Post, RejectionReason, State, TopicStatus
from factory.core.logging import get_logger

log = get_logger(__name__)

# Rejecting a published post would leave it live in the group while the database
# claims it was thrown away — worse than refusing.
REJECTABLE_STATES = frozenset(
    {
        State.QUEUED,
        State.TEXT_READY,
        State.FACTCHECKED,
        State.PROMPTS_READY,
        State.IMAGES_READY,
        State.COMPOSED,
        State.IN_REVIEW,
        State.APPROVED,
        State.FAILED,
    }
)


def snapshot_of(conn: sqlite3.Connection, post: Post) -> str:
    """What the post looked like when it was thrown away."""
    assets = conn.execute(
        "SELECT kind, position, prompt, seed FROM assets WHERE post_id = ? ORDER BY position",
        (post.id,),
    ).fetchall()

    return json.dumps(
        {
            "title": post.title,
            "body": post.body,
            "question": post.question,
            "factcheck_verdict": post.factcheck_verdict,
            "state_when_rejected": post.state,
            "prompts": [
                {
                    "kind": row["kind"],
                    "position": row["position"],
                    "prompt": row["prompt"],
                    "seed": row["seed"],
                }
                for row in assets
            ],
        },
        ensure_ascii=False,
    )


def reject_post(conn: sqlite3.Connection, post_id: int, *, reason: str) -> None:
    """Move a post to ``rejected`` and free its topic, in one transaction."""
    if reason not in set(RejectionReason):
        raise FactoryError(
            f"Неизвестная причина отклонения: '{reason}'.",
            why=f"Допустимые значения: {', '.join(sorted(RejectionReason))}.",
            what_to_do="Укажи одну из допустимых причин.",
        )

    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        raise FactoryError(
            f"Пост {post_id} не найден.",
            why="Такого номера нет в базе.",
            what_to_do="Посмотри список постов: factory post list",
        )

    post = Post.from_row(row)

    if post.state == State.REJECTED:
        log.info("пост уже отклонён", extra={"post_id": post_id})
        return

    if post.state not in REJECTABLE_STATES:
        raise FactoryError(
            f"Пост {post_id} нельзя отклонить: он в состоянии '{post.state}'.",
            why=(
                "Опубликованный пост уже виден подписчикам, и пометка «в мусор» "
                "в базе его оттуда не уберёт."
            ),
            what_to_do="Удали пост в самой группе ВКонтакте, если он больше не нужен.",
        )

    stamp = to_iso(now_utc())
    snapshot = snapshot_of(conn, post)

    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO rejections (post_id, reason, snapshot, created_at) VALUES (?, ?, ?, ?)",
            (post_id, reason, snapshot, stamp),
        )
        conn.execute(
            "UPDATE posts SET state = ?, updated_at = ? WHERE id = ?",
            (State.REJECTED, stamp, post_id),
        )
        conn.execute(
            "UPDATE topics SET status = ?, used_at = NULL WHERE id = ?",
            (TopicStatus.FREE, post.topic_id),
        )

    log.info(
        "пост отклонён, тема возвращена в очередь",
        extra={"post_id": post_id, "topic_id": post.topic_id, "reason": reason},
    )
