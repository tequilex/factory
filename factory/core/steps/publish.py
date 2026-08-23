"""approved -> published: the only step that cannot be undone.

Gates before anything leaves the building:

* ``external_id IS NULL`` — the post has not been published already;
* the daily limit, counted on ``published_at`` — never on ``updated_at``, which
  changes on any edit;
* the schedule: the current time is inside a slot, and that slot has not been
  used yet. Without the second half both of the day's posts go out in the same
  hour and the later slot stays empty.

The schedule and limit gates return ``WAITING``: nothing is wrong with the post,
it is simply not its turn.

**What is and is not guaranteed about duplicates.** The step never retries — see
``attempts=1`` below — because a publish call that times out has most likely
already succeeded on the far side. The ``UPDATE ... AND external_id IS NULL``
protects the database record against a second tick. What is *not* covered is the
window between the call returning and the row being written: a kill in there
leaves the post live in the group with nothing recorded, and the next tick will
publish it again. Closing that needs an idempotency token the provider honours —
VK's ``wall.post`` takes ``guid`` — and that lands with the real publisher in
Stage 2. Until then the tick lock is the only thing narrowing the window, and
this comment is deliberately explicit so nobody assumes otherwise.
"""

from __future__ import annotations

import shutil
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


def slot_already_used(
    conn: sqlite3.Connection, project: ProjectConfig, project_id: int, moment: datetime, slot: time
) -> bool:
    """Whether something has already gone out in this slot.

    One post per slot. Without this the whole day's allowance empties into the
    first open window: two posts ten minutes apart in the evening, nothing at the
    later time the owner actually chose.
    """
    local = moment.astimezone(project.vk.tz)
    start = local.replace(hour=slot.hour, minute=slot.minute, second=0, microsecond=0)
    end = start + timedelta(minutes=SLOT_WINDOW_MIN)

    rows = conn.execute(
        "SELECT published_at FROM posts WHERE project_id = ? AND published_at IS NOT NULL",
        (project_id,),
    ).fetchall()
    return any(
        start <= from_iso(row["published_at"]).astimezone(project.vk.tz) < end for row in rows
    )


def _assets(conn: sqlite3.Connection, post_id: int) -> list[Asset]:
    """Post attachments, cover first.

    Order matters: it becomes the order of the attachment string, and VK shows the
    first image as the main one. Sorting by ``kind`` would put ``inline`` ahead of
    ``cover`` — alphabetically 'cover' < 'inline' — and the headline image would
    end up last.
    """
    rows = conn.execute(
        "SELECT * FROM assets WHERE post_id = ? ORDER BY position", (post_id,)
    ).fetchall()
    return [Asset.from_row(row) for row in rows]


def _cleanup_files(post_id: int, log) -> None:
    """Remove the post's images once it is out.

    Failures here are logged, never raised: the post is already published, and
    refusing to record that because a file would not delete would be far worse
    than leaving the file behind.
    """
    target = paths.post_tmp_dir(post_id)
    try:
        shutil.rmtree(target, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning(
            "не удалось удалить временные файлы поста",
            extra={"post_id": post_id, "path": str(target), "reason": str(exc)},
        )


# attempts=1: publishing is irreversible. A timeout on the call does not mean the
# post was not created — retrying would put a second copy in the group.
@tracked_call(State.APPROVED, attempts=1)
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

    if not paths.ignore_schedule():
        moment = now_utc()
        slot = open_slot(ctx.project, moment)
        if slot is None:
            return waiting(f"вне расписания публикаций {ctx.project.vk.schedule}")
        if slot_already_used(ctx.conn, ctx.project, project_id, moment, slot):
            return waiting(f"в слоте {slot:%H:%M} уже была публикация")

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

    _cleanup_files(ctx.post.id, ctx.log)

    ctx.log.info(
        "пост опубликован",
        extra={"post_id": ctx.post.id, "external_id": external_id},
    )
    return advanced(State.PUBLISHED)
