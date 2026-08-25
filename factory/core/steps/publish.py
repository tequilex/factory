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

**How duplicates are prevented.** Three layers, each covering what the previous
one cannot:

1. the step never retries (``attempts=1`` below) — a publish call that times out
   has most likely already succeeded on the far side;
2. ``UPDATE ... AND external_id IS NULL`` protects the database record against a
   second tick that got there first;
3. ``publish_guid`` is written **before** the call and passed to the provider.
   This is the layer that covers the gap the other two cannot: a kill between the
   call returning and the row being written. On the next attempt the same guid
   goes out, and VK returns the existing post instead of creating a second one.

Layer 3 is why the guid is generated here and not inside the provider: it has to
survive a process death, which means it has to be in the database first.
"""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from datetime import datetime, time, timedelta

from factory.core import alerts, db, paths
from factory.core.clock import from_iso, now_utc, to_iso
from factory.core.config import ProjectConfig
from factory.core.models import Asset, Post, State, TopicStatus
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


def _slot_start(local: datetime, slot: time, day_offset: int = 0) -> datetime:
    """Момент открытия слота на сутках, сдвинутых на ``day_offset`` дней.

    Считается через дату, а не через ``replace``: у слота 23:30 окно в час
    заканчивается уже в следующих сутках, и привязка только к сегодняшней дате
    обрезала бы его до полуночи. Для 23:55 «реальное» окно оказалось бы пятью
    минутами, и один пропущенный тик молча съедал бы дневную публикацию.
    """
    day = (local + timedelta(days=day_offset)).date()
    naive = datetime.combine(day, slot)
    # localize через tzinfo самого local: так переход на летнее время не сдвигает
    # окно на час.
    return naive.replace(tzinfo=local.tzinfo)


def open_slot(project: ProjectConfig, moment: datetime) -> time | None:
    """The schedule slot the given moment falls into, if any."""
    local = moment.astimezone(project.vk.tz)
    for slot in project.vk.slots:
        # -1 нужен для слотов, чьё окно перешагнуло полночь: в 00:10 открыт
        # вчерашний слот 23:30, а не сегодняшний.
        for day_offset in (0, -1):
            start = _slot_start(local, slot, day_offset)
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
    # То же окно, что нашёл open_slot: со сдвигом на сутки назад, если полночь
    # уже пройдена.
    start = _slot_start(local, slot)
    if start > local:
        start = _slot_start(local, slot, -1)
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

    from factory.core.topics import schedule_is_off

    if not schedule_is_off(ctx.conn, ctx.project.slug):
        moment = now_utc()
        slot = open_slot(ctx.project, moment)
        if slot is None:
            return waiting(f"вне расписания публикаций {ctx.project.vk.schedule}")
        if slot_already_used(ctx.conn, ctx.project, project_id, moment, slot):
            return waiting(f"в слоте {slot:%H:%M} уже была публикация")

    assets = _assets(ctx.conn, ctx.post.id)

    # guid записывается ДО отправки. Если ответ не доедет, а пост уже создан,
    # следующая попытка пойдёт с тем же guid — и ВК вернёт существующий пост
    # вместо второго. Сгенерировать его после вызова было бы бессмысленно:
    # именно окно между отправкой и записью мы и закрываем.
    post = ctx.post
    if not post.publish_guid:
        guid = uuid.uuid4().hex
        with db.write_transaction(ctx.conn):
            ctx.conn.execute(
                "UPDATE posts SET publish_guid = ? WHERE id = ? AND publish_guid IS NULL",
                (guid, post.id),
            )
        post = Post.from_row(
            ctx.conn.execute("SELECT * FROM posts WHERE id = ?", (post.id,)).fetchone()
        )

    external_id = ctx.providers.publisher.publish(post, assets)

    # Публикация прошла — значит ключ загрузки рабочий. Тревогу о протухшем
    # ключе снимаем здесь, а не только при обновлении через бота: ключ могли
    # заменить руками по RUNBOOK, и тогда завтрашнее протухание прошло бы молча.
    alerts.clear(ctx.conn, "vk_token", ctx.project.slug)

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
    _tell_the_owner(ctx, external_id)
    return advanced(State.PUBLISHED)


def post_url(external_id: str) -> str:
    """Ссылка на запись в группе. ``external_id`` — это ``{-group_id}_{post_id}``."""
    return f"https://vk.com/wall{external_id}"


def _tell_the_owner(ctx: StepContext, external_id: str) -> None:
    """Сказать в Telegram, что пост вышел, и снять кнопку отмены.

    Отменять больше нечего: удалять записи в группе система не умеет и не
    должна. Оставить кнопку значило бы обещать то, чего она не сделает.
    """
    if ctx.project.telegram is None or not ctx.post.review_message_id:
        return

    try:
        ctx.providers.notifier.finish_review(
            chat_id=ctx.post.review_chat_id or ctx.project.telegram.chat_id,
            message_id=ctx.post.review_message_id,
            text=f"📣 Опубликовано: {post_url(external_id)}",
        )
    except Exception as exc:  # noqa: BLE001 — пост уже вышел, это уведомление
        ctx.log.warning(
            "не удалось сообщить о публикации",
            extra={"post_id": ctx.post.id, "reason": str(exc)},
        )


def next_slot_start(project: ProjectConfig, moment: datetime) -> datetime | None:
    """Ближайший момент, когда пост сможет выйти. ``None`` — расписания нет.

    Нужно боту: «уйдёт в ближайший слот» без времени владелец прочитать не может,
    а пауза до вечера выглядит как поломка.
    """
    if not project.vk.slots:
        return None

    local = moment.astimezone(project.vk.tz)
    starts = [
        _slot_start(local, slot, offset)
        for slot in project.vk.slots
        for offset in (0, 1)
    ]
    return min((start for start in starts if start > local), default=None)
