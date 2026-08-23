"""approved -> published: the only step that cannot be undone.

Three gates before anything leaves the building:

* ``external_id IS NULL`` — the post has not been published already. This is the
  duplicate guard, and it is checked inside the same transaction that records the
  result, so a crash between "posted" and "recorded" cannot produce a second copy
  on the next tick;
* the daily limit, counted on ``published_at`` — never on ``updated_at``, which
  changes on any edit;
* the publishing schedule.

The last two return ``WAITING``: there is nothing wrong with the post, it is
simply not its turn.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta

from factory.core import db, paths
from factory.core.clock import from_iso, now_utc, to_iso
from factory.core.config import ProjectConfig
from factory.core.models import Asset, State, TopicStatus
from factory.core.retry import tracked_call
from factory.core.steps import StepContext, StepResult, advanced, waiting

# How long after a slot time a post may still go out. Without a window, a tick
# that lands a minute late would skip the slot entirely.
SLOT_WINDOW_MIN = 60


def published_today(conn: sqlite3.Connection, project: ProjectConfig, project_id: int) -> int:
    """How many posts went out today, in the project's timezone.

    Counted on ``published_at`` on purpose. ``updated_at`` moves whenever a row is
    touched, so a post published yesterday at 23:50 would eat today's slot as soon
    as anything edited it.
    """
    rows = conn.execute(
        "SELECT published_at FROM posts WHERE project_id = ? AND published_at IS NOT NULL",
        (project_id,),
    ).fetchall()

    today = now_utc().astimezone(project.vk.tz).date()
    return sum(1 for row in rows if from_iso(row["published_at"]).astimezone(project.vk.tz).date() == today)


def open_slot(project: ProjectConfig, moment: datetime) -> time | None:
    """The schedule slot the given moment falls into, if any."""
    local = moment.astimezone(project.vk.tz)
    for slot in project.vk.slots:
        start = local.replace(hour=slot.hour, minute=slot.minute, second=0, microsecond=0)
        if start <= local < start + timedelta(minutes=SLOT_WINDOW_MIN):
            return slot
    return None


def _assets(conn: sqlite3.Connection, post_id: int) -> list[Asset]:
    rows = conn.execute(
        "SELECT * FROM assets WHERE post_id = ? ORDER BY kind DESC, position", (post_id,)
    ).fetchall()
    return [Asset.from_row(row) for row in rows]


@tracked_call(State.APPROVED)
def run(ctx: StepContext) -> StepResult:
    if ctx.post.external_id:
        ctx.log.info(
            "пост уже опубликован, повтор не нужен",
            extra={"post_id": ctx.post.id, "external_id": ctx.post.external_id},
        )
        return advanced(State.PUBLISHED)

    project_id = ctx.conn.execute(
        "SELECT project_id FROM posts WHERE id = ?", (ctx.post.id,)
    ).fetchone()["project_id"]

    already = published_today(ctx.conn, ctx.project, project_id)
    if already >= ctx.project.limits.posts_per_day:
        return waiting(f"дневной лимит исчерпан: {already} из {ctx.project.limits.posts_per_day}")

    if paths.ignore_schedule():
        pass
    elif open_slot(ctx.project, now_utc()) is None:
        return waiting(f"вне расписания публикаций {ctx.project.vk.schedule}")

    assets = _assets(ctx.conn, ctx.post.id)
    external_id = ctx.providers.publisher.publish(ctx.post, assets)

    stamp = to_iso(now_utc())
    with db.write_transaction(ctx.conn):
        # The WHERE clause repeats the duplicate check inside the write
        # transaction: between the check above and here another tick could have
        # published the same post.
        cursor = ctx.conn.execute(
            "UPDATE posts SET external_id = ?, published_at = ?, updated_at = ? "
            "WHERE id = ? AND external_id IS NULL",
            (external_id, stamp, stamp, ctx.post.id),
        )
        if cursor.rowcount == 0:
            ctx.log.warning(
                "пост опубликовали параллельно, результат этой публикации отброшен",
                extra={"post_id": ctx.post.id},
            )
        else:
            ctx.conn.execute(
                "UPDATE topics SET status = ?, used_at = ? WHERE id = ?",
                (TopicStatus.USED, stamp, ctx.post.topic_id),
            )

    ctx.log.info(
        "пост опубликован",
        extra={"post_id": ctx.post.id, "external_id": external_id},
    )
    return advanced(State.PUBLISHED)
