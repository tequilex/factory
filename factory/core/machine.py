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

from factory.core import alerts, db, lock, paths
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


def _remember_warning(conn: sqlite3.Connection, key: str, value: str) -> bool:
    """Записывает состояние и говорит, изменилось ли оно с прошлого раза.

    Нужно, чтобы предупреждение писалось на смене состояния, а не каждый тик.
    Раз в десять минут вечно — это 144 строки в сутки на проект, и совет из
    RUNBOOK «смотри WARNING и ERROR в логах» перестаёт работать.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is not None and row["value"] == value:
        return False

    with db.write_transaction(conn):
        conn.execute(lock.UPSERT_META, (key, value, to_iso(now_utc())))
    return True


def replenish_queue(conn: sqlite3.Connection, project: Project, config: ProjectConfig) -> int:
    """Create posts until the buffer is full. Returns how many were created."""
    created = 0
    ran_out = False

    while in_flight(conn, project.id) < config.limits.queue_buffer:
        if create_post_for_next_topic(conn, project) is None:
            ran_out = True
            break
        created += 1

    state_key = f"topics_exhausted:{project.slug}"
    if _remember_warning(conn, state_key, "1" if ran_out else "0") and ran_out:
        log.warning(
            "свободные темы закончились",
            extra={
                "project": project.slug,
                "in_flight": in_flight(conn, project.id),
                "what_to_do": f"factory topics import {project.slug} <файл>",
            },
        )

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
            if getattr(exc, "token_expired", False):
                # Истёкший ключ ретраями не лечится: он не станет действительным
                # сам. Считать это ошибкой значит сжигать бюджет попыток на
                # ожидание человека — пост умирает за час, пока владелец спит,
                # хотя достаточно было дождаться нового ключа. Это ровно то, для
                # чего существует WAITING.
                try:
                    _alert_if_hopeless(conn, config, providers, exc)
                except Exception:  # noqa: BLE001 — уведомление о поломке не поломка
                    log.exception("не удалось позвать владельца")
                record_wait(conn, post, str(exc).splitlines()[0])
                break

            record_failure(conn, post, exc)
            try:
                _alert_if_hopeless(conn, config, providers, exc)
            except Exception:  # noqa: BLE001 — уведомление о поломке не поломка
                log.exception("не удалось позвать владельца")
            break

        if result.outcome is Outcome.WAITING:
            record_wait(conn, post, result.reason)
            break

        commit_transition(conn, post, result.next_state)
        done += 1
        post = reload_post(conn, post.id)

    return done


def _alert_if_hopeless(conn: sqlite3.Connection, config, providers, exc: BaseException) -> None:
    """Сообщить владельцу о поломке, которую система сама не переживёт.

    Протухший ключ ВК ретраями не лечится: он не станет действительным сам, и
    пять попыток просто израсходуют бюджет поста впустую. Единственное, что тут
    помогает, — человек, а значит человека надо позвать, а не писать в лог,
    который он не читает.
    """
    token_env = getattr(exc, "token_env", None)
    if not getattr(exc, "token_expired", False) or config.telegram is None:
        return

    alerts.raise_once(
        conn,
        providers.notifier,
        chat_id=config.telegram.chat_id,
        name="vk_token",
        scope=config.slug,
        text=alerts.vk_token_expired_text(
            config.slug, token_env or "VK_UPLOAD_TOKEN", config.vk.app_id
        ),
    )


def _free_topics(conn: sqlite3.Connection, project_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM topics WHERE project_id = ? AND status = ?",
        (project_id, TopicStatus.FREE),
    ).fetchone()[0]


def check_alerts(conn: sqlite3.Connection, project, config, providers) -> None:
    """Позвать владельца, если система встала и сама не выберется.

    Вызывается раз в тик на проект. Каждая тревога звучит один раз и снимается,
    когда причина исчезла: тик идёт раз в минуту, и без этого получился бы поток
    одинаковых сообщений, который перестают читать за час.

    Тревоги на «N постов ждут ревью» тут намеренно нет — это нормальная работа,
    а не авария, и именно так и появляется шум.
    """
    if config.telegram is None:
        return

    chat_id = config.telegram.chat_id
    _alert_nothing_to_publish(conn, project, config, providers, chat_id)
    _alert_stuck_posts(conn, project, config, providers, chat_id)
    _alert_failed_posts(conn, project, config, providers, chat_id)


def _alert_nothing_to_publish(conn, project, config, providers, chat_id: int) -> None:
    free = _free_topics(conn, project.id)
    working = in_flight(conn, project.id)

    # Тревога не на «темы кончились», а на «постов в работе меньше, чем система
    # выпускает за сутки». Пока запас есть, кончившиеся темы — не срочность.
    hopeless = free == 0 and working < config.limits.posts_per_day

    if not hopeless:
        alerts.clear(conn, "no_topics", config.slug)
        return

    alerts.raise_once(
        conn, providers.notifier, chat_id=chat_id, name="no_topics", scope=config.slug,
        text=alerts.nothing_to_publish_text(config.slug, free, working),
    )


def _alert_stuck_posts(conn, project, config, providers, chat_id: int) -> None:
    """Пост, простоявший на одном месте дольше суток.

    В ``failed`` такой пост не переводится: ожидание человека это не ошибка, и
    убивать по таймауту пост, который владелец просто не успел посмотреть, —
    худшее, что можно сделать.
    """
    threshold = to_iso(now_utc() - timedelta(hours=alerts.STUCK_AFTER_HOURS))
    # approved исключён намеренно: одобренный пост ждёт своего слота, и это
    # работа, а не застревание. При queue_buffer = posts_per_day × 3 владелец
    # одобряет за один заход больше постов, чем выходит за сутки, — тревога на
    # них стала бы ровно тем шумом, из-за которого отказались от алерта
    # «N постов ждут ревью». Протухший ключ на этом шаге виден по своей тревоге.
    quiet = set(TERMINAL_STATES) | {State.APPROVED}
    placeholders = ", ".join("?" for _ in quiet)
    rows = conn.execute(
        f"SELECT id, state, title FROM posts WHERE project_id = ? "
        f"AND state NOT IN ({placeholders}) AND updated_at <= ? ORDER BY id",
        (project.id, *sorted(quiet), threshold),
    ).fetchall()

    for row in rows:
        alerts.raise_once(
            conn, providers.notifier, chat_id=chat_id,
            name="stuck", scope=f"{config.slug}:{row['id']}",
            text=alerts.stuck_post_text(
                config.slug, row["id"], row["state"], alerts.STUCK_AFTER_HOURS, row["title"]
            ),
        )


def _alert_failed_posts(conn, project, config, providers, chat_id: int) -> None:
    rows = conn.execute(
        "SELECT id, title, last_error FROM posts WHERE project_id = ? AND state = ? ORDER BY id",
        (project.id, State.FAILED),
    ).fetchall()

    for row in rows:
        alerts.raise_once(
            conn, providers.notifier, chat_id=chat_id,
            name="failed", scope=f"{config.slug}:{row['id']}",
            text=alerts.failed_post_text(
                config.slug, row["id"], row["title"], row["last_error"]
            ),
            fix_post_id=row["id"],
        )


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
                # build_providers must be inside the same guard: a provider name
                # that is valid but not yet implemented raises here, and outside
                # the guard that would abort the whole tick — every other project
                # would stop, and the heartbeat below would never be written, so
                # the owner would be told "the worker is dead" instead of "this
                # project has a bad provider".
                providers = build_providers(config)
            except FactoryError as exc:
                log.error("проект пропущен", extra={"project": project.slug, "error": str(exc)})
                continue

            summary["projects"] += 1
            summary["posts_created"] += replenish_queue(conn, project, config)

            for post in due_posts(conn, project.id):
                summary["advanced"] += advance_post(conn, post, config, providers)
                lock.refresh(conn)

            try:
                check_alerts(conn, project, config, providers)
            except FactoryError as exc:
                # Не сумели позвать владельца — плохо, но это не повод ронять
                # тик: посты важнее уведомлений о постах.
                log.warning(
                    "не удалось проверить тревоги",
                    extra={"project": project.slug, "error": str(exc)},
                )

        lock.write_heartbeat(conn)

    log.info("тик завершён", extra=summary)
    return summary
