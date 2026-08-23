"""The tick: the loop that moves every post forward.

One tick does three things, in order:

1. take the lock — never two ticks at once;
2. top each active project's queue up to ``limits.queue_buffer``;
3. move every post that is due forward by up to ``FACTORY_MAX_STEPS_PER_TICK``
   steps, committing after each one.

The commit-per-step is the whole design. Killed with ``-9`` between two steps,
the work already done stays done, and the next tick picks up exactly where this
one stopped. Nothing that has been paid for is ever bought twice.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from factory.core import db, lock, paths
from factory.core.clock import now_utc, to_iso
from factory.core.config import ProjectConfig, load_project
from factory.core.errors import FactoryError
from factory.core.logging import get_logger
from factory.core.models import TERMINAL_STATES, Post, Project, State, TopicStatus
from factory.core.steps import Outcome, StepContext, handler_for
from factory.providers.registry import build_providers

# SPEC.md: 10 минут × 2^retry_count, максимум 6 часов.
BACKOFF_BASE_SEC = 600
BACKOFF_CAP_SEC = 6 * 3600
MAX_RETRIES = 5

log = get_logger(__name__)


def backoff_sec(retry_count: int) -> int:
    """Pause before the next attempt, after ``retry_count`` failures.

    Only four values are ever used in practice — 10, 20, 40 and 80 minutes. The
    fifth failure moves the post to ``failed``, so the cap exists for the day
    someone raises :data:`MAX_RETRIES`, not for today.
    """
    return min(BACKOFF_BASE_SEC * 2 ** max(0, retry_count - 1), BACKOFF_CAP_SEC)


def active_projects(conn: sqlite3.Connection) -> list[Project]:
    rows = conn.execute("SELECT * FROM projects WHERE is_active = 1 ORDER BY id").fetchall()
    return [Project.from_row(row) for row in rows]


def in_flight(conn: sqlite3.Connection, project_id: int) -> int:
    """Posts still on their way. Terminal states free up a slot in the buffer."""
    placeholders = ", ".join("?" for _ in TERMINAL_STATES)
    row = conn.execute(
        f"SELECT COUNT(*) FROM posts WHERE project_id = ? AND state NOT IN ({placeholders})",
        (project_id, *sorted(TERMINAL_STATES)),
    ).fetchone()
    return int(row[0])


def _claim_locked(conn: sqlite3.Connection, project_id: int) -> int | None:
    """Take one free topic. Must be called inside a write transaction.

    A single ``UPDATE ... RETURNING`` rather than a select followed by an update,
    so two ticks racing here cannot both walk away with the same topic.
    """
    row = conn.execute(
        "UPDATE topics SET status = ? WHERE id = ("
        "  SELECT id FROM topics WHERE project_id = ? AND status = ? ORDER BY id LIMIT 1"
        ") RETURNING id",
        (TopicStatus.TAKEN, project_id, TopicStatus.FREE),
    ).fetchone()
    return int(row["id"]) if row else None


def claim_free_topic(conn: sqlite3.Connection, project_id: int) -> int | None:
    """Take one free topic in its own transaction. Returns its id, or ``None``."""
    with db.write_transaction(conn):
        return _claim_locked(conn, project_id)


def attempts_for_topic(conn: sqlite3.Connection, topic_id: int) -> int:
    """How many times this topic has been rejected before.

    Feeds the third segment of ``idem_key``: a rejected topic goes back to
    ``free`` and gets a fresh post, which the unique index would otherwise block.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM rejections r JOIN posts p ON p.id = r.post_id WHERE p.topic_id = ?",
        (topic_id,),
    ).fetchone()
    return int(row[0])


def create_post_for_next_topic(conn: sqlite3.Connection, project: Project) -> int | None:
    """Claim a topic and create its post — in one transaction.

    These two must not be split. Two transactions leak: a crash in between leaves
    a topic marked ``taken`` with no post to show for it, and nothing ever puts it
    back. Together they either both happen or neither does.

    Returns the new post id, or ``None`` when no topic was free.
    """
    stamp = to_iso(now_utc())
    with db.write_transaction(conn):
        topic_id = _claim_locked(conn, project.id)
        if topic_id is None:
            return None

        attempt = attempts_for_topic(conn, topic_id)
        cursor = conn.execute(
            "INSERT INTO posts (project_id, topic_id, idem_key, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                project.id,
                topic_id,
                f"{project.slug}:{topic_id}:{attempt}",
                State.QUEUED,
                stamp,
                stamp,
            ),
        )
        post_id = int(cursor.lastrowid)

    log.info(
        "пост создан",
        extra={"project": project.slug, "post_id": post_id, "topic_id": topic_id},
    )
    return post_id


def replenish_queue(conn: sqlite3.Connection, project: Project, config: ProjectConfig) -> int:
    """Create posts until the buffer is full. Returns how many were created."""
    created = 0
    while in_flight(conn, project.id) < config.limits.queue_buffer:
        if create_post_for_next_topic(conn, project) is None:
            log.warning(
                "свободные темы закончились",
                extra={"project": project.slug, "in_flight": in_flight(conn, project.id)},
            )
            break
        created += 1
    return created


def due_posts(conn: sqlite3.Connection, project_id: int) -> list[Post]:
    """Non-terminal posts whose backoff has expired, oldest first."""
    placeholders = ", ".join("?" for _ in TERMINAL_STATES)
    rows = conn.execute(
        f"SELECT * FROM posts WHERE project_id = ? AND state NOT IN ({placeholders}) "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id",
        (project_id, *sorted(TERMINAL_STATES), to_iso(now_utc())),
    ).fetchall()
    return [Post.from_row(row) for row in rows]


def reload_post(conn: sqlite3.Connection, post_id: int) -> Post:
    return Post.from_row(conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone())


def commit_transition(conn: sqlite3.Connection, post: Post, next_state: str) -> None:
    """Record one completed step. Its own transaction, on purpose."""
    with db.write_transaction(conn):
        conn.execute(
            "UPDATE posts SET state = ?, retry_count = 0, last_error = NULL, "
            "next_attempt_at = NULL, updated_at = ? WHERE id = ?",
            (next_state, to_iso(now_utc()), post.id),
        )


def record_wait(conn: sqlite3.Connection, post: Post, reason: str | None) -> None:
    """A post waiting on the outside world. Not an error: ``retry_count`` untouched.

    ``next_attempt_at`` is nudged one tick forward so the post is not re-examined
    pointlessly within the same minute, but its retry budget stays whole — a post
    in review over a weekend must not die of old age.
    """
    resume_at = now_utc() + timedelta(seconds=paths.tick_interval_sec())
    with db.write_transaction(conn):
        conn.execute(
            "UPDATE posts SET next_attempt_at = ?, last_error = NULL WHERE id = ?",
            (to_iso(resume_at), post.id),
        )
    log.info("пост ждёт", extra={"post_id": post.id, "state": post.state, "reason": reason})


def record_failure(conn: sqlite3.Connection, post: Post, exc: BaseException) -> None:
    """Count a failure and schedule the retry, or give up after :data:`MAX_RETRIES`."""
    retry_count = post.retry_count + 1
    message = str(exc) if isinstance(exc, FactoryError) else f"{type(exc).__name__}: {exc}"

    if retry_count >= MAX_RETRIES:
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET state = ?, retry_count = ?, last_error = ?, "
                "next_attempt_at = NULL, updated_at = ? WHERE id = ?",
                (State.FAILED, retry_count, message, to_iso(now_utc()), post.id),
            )
        log.error(
            "пост переведён в failed после исчерпания попыток",
            extra={"post_id": post.id, "state": post.state, "retry_count": retry_count},
        )
        return

    next_attempt = now_utc() + timedelta(seconds=backoff_sec(retry_count))
    with db.write_transaction(conn):
        conn.execute(
            "UPDATE posts SET retry_count = ?, last_error = ?, next_attempt_at = ?, "
            "updated_at = ? WHERE id = ?",
            (retry_count, message, to_iso(next_attempt), to_iso(now_utc()), post.id),
        )
    log.warning(
        "шаг не удался, попробуем позже",
        extra={
            "post_id": post.id,
            "state": post.state,
            "retry_count": retry_count,
            "next_attempt_at": to_iso(next_attempt),
        },
    )


def advance_post(
    conn: sqlite3.Connection,
    post: Post,
    config: ProjectConfig,
    providers,
    *,
    max_steps: int | None = None,
) -> int:
    """Move one post forward. Returns how many steps it actually took.

    The chain stops on an error, on ``WAITING``, on reaching a terminal state, or
    on running out of steps. Every completed step is committed before the next one
    starts, so a crash anywhere in here loses at most the step in progress.
    """
    limit = max_steps if max_steps is not None else paths.max_steps_per_tick()
    done = 0

    for _ in range(limit):
        if post.is_terminal:
            break

        ctx = StepContext(
            conn=conn,
            project=config,
            post=post,
            providers=providers,
            log=log,
        )

        try:
            result = handler_for(post.state)(ctx)
        except Exception as exc:  # noqa: BLE001 — recorded, then the chain stops
            record_failure(conn, post, exc)
            break

        if result.outcome is Outcome.WAITING:
            record_wait(conn, post, result.reason)
            break

        commit_transition(conn, post, result.next_state)
        done += 1
        post = reload_post(conn, post.id)

    return done


def tick(conn: sqlite3.Connection) -> dict:
    """One pass over everything. Returns a small summary for the CLI and tests."""
    summary = {"projects": 0, "posts_created": 0, "advanced": 0, "skipped": False}

    if paths.ignore_schedule():
        log.warning(
            "расписание отключено переменной FACTORY_IGNORE_SCHEDULE — "
            "посты публикуются в любое время; в боевом конфиге этого быть не должно"
        )

    with lock.tick_lock(conn) as acquired:
        if not acquired:
            summary["skipped"] = True
            return summary

        for project in active_projects(conn):
            try:
                config = load_project(project.slug)
            except FactoryError as exc:
                # One broken config must not stop the other projects.
                log.error("проект пропущен", extra={"project": project.slug, "error": str(exc)})
                continue

            providers = build_providers(config)
            summary["projects"] += 1
            summary["posts_created"] += replenish_queue(conn, project, config)

            for post in due_posts(conn, project.id):
                summary["advanced"] += advance_post(conn, post, config, providers)
                lock.refresh(conn)

        lock.write_heartbeat(conn)

    log.info("тик завершён", extra=summary)
    return summary
