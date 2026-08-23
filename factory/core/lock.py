"""Tick lock and heartbeat, both stored as rows in ``meta``.

Two requirements pull against each other: two ticks must never run at once, but a
process killed with ``kill -9`` must not wedge the system until someone notices.
The lock therefore carries an expiry, is refreshed while work is in progress, and
a tick that finds an expired lock takes it over.

Holding the lock in the database rather than in a file means it travels with the
data directory and works the same in Docker, under systemd and in tests.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from factory.core import db, paths
from factory.core.clock import from_iso, now_utc, to_iso
from factory.core.errors import LockError
from factory.core.logging import get_logger

LOCK_KEY = "tick_lock"
HEARTBEAT_KEY = "heartbeat"

# SPEC.md, «Эксплуатация»: если тик не отработал успешно два часа — будить владельца.
HEARTBEAT_STALE_AFTER_SEC = 2 * 3600

log = get_logger(__name__)


# Regenerated on every start. Hostname plus PID is not enough to identify a
# process: in Docker the worker is always PID 1 and the container hostname is
# stable across restarts, so a process killed with -9 and its replacement would
# look identical. Ownership checks would then silently pass for the wrong process.
_PROCESS_TOKEN = uuid.uuid4().hex


def _identity() -> dict[str, object]:
    return {"holder": socket.gethostname(), "pid": os.getpid(), "token": _PROCESS_TOKEN}


def _is_ours(payload: dict | None) -> bool:
    if payload is None:
        return False
    mine = _identity()
    return all(payload.get(key) == value for key, value in mine.items())


def _payload(expires_at: datetime) -> str:
    return json.dumps({**_identity(), "expires_at": to_iso(expires_at)}, ensure_ascii=False)


UPSERT_META = (
    "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
)


def _store(conn: sqlite3.Connection, payload: str) -> None:
    """Write the lock row. Caller must already hold a write transaction."""
    conn.execute(UPSERT_META, (LOCK_KEY, payload, to_iso(now_utc())))


def _parse(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    try:
        payload = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        # A corrupted row must not wedge the worker every ten minutes forever.
        log.warning("запись блокировки повреждена, будет перехвачена", extra={"raw": row["value"]})
        return None
    return payload if isinstance(payload, dict) else None


def _read(conn: sqlite3.Connection) -> dict | None:
    return _parse(conn.execute("SELECT value FROM meta WHERE key = ?", (LOCK_KEY,)).fetchone())


def _is_expired(payload: dict | None) -> bool:
    if payload is None:
        return True
    try:
        return from_iso(payload["expires_at"]) <= now_utc()
    except (KeyError, ValueError):
        return True


def _try_acquire(conn: sqlite3.Connection) -> bool:
    """Take the lock, or take it over if the current one has expired.

    Read and write happen inside one ``BEGIN IMMEDIATE`` so two ticks racing here
    cannot both conclude the lock is free.
    """
    expires_at = now_utc() + timedelta(seconds=paths.lock_ttl_sec())

    with db.write_transaction(conn):
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (LOCK_KEY,)).fetchone()
        payload = _parse(row)

        if row is not None and not _is_expired(payload):
            return False

        if row is not None:
            log.warning(
                "блокировка предыдущего тика протухла и перехвачена",
                extra={"previous": payload},
            )

        _store(conn, _payload(expires_at))
        return True


def _release(conn: sqlite3.Connection) -> None:
    """Drop the lock, but only if it is still ours.

    A tick that overran its TTL may have had the lock taken over by the next one.
    Deleting unconditionally would then remove somebody else's lock and allow a
    third tick to start alongside.

    The ownership check runs in Python rather than via ``json_extract`` so the
    code does not depend on SQLite being compiled with the JSON1 extension —
    the target device is whatever ARM build happens to be installed.
    """
    with db.write_transaction(conn):
        payload = _parse(
            conn.execute("SELECT value FROM meta WHERE key = ?", (LOCK_KEY,)).fetchone()
        )
        if _is_ours(payload):
            conn.execute("DELETE FROM meta WHERE key = ?", (LOCK_KEY,))
        elif payload is not None:
            log.warning(
                "блокировка уже не наша, снимать не будем",
                extra={"current": payload},
            )


@contextmanager
def tick_lock(conn: sqlite3.Connection) -> Iterator[bool]:
    """Guard around one tick.

    Yields ``True`` if the lock was acquired and the caller should do the work,
    ``False`` if another tick is already running. A refused caller must return
    without touching anything.
    """
    acquired = _try_acquire(conn)
    if not acquired:
        current = _read(conn)
        log.info("тик пропущен: работает другой процесс", extra={"lock": current})
        yield False
        return

    try:
        yield True
    finally:
        _release(conn)


def refresh(conn: sqlite3.Connection) -> None:
    """Push the expiry forward while a long tick is still working.

    Called between posts, so a slow tick never has its lock stolen mid-flight.

    Read, ownership check and write must all happen inside **one** transaction.
    Splitting them opens a window in which our lock expires, another tick takes it
    over, and we then overwrite that tick's row with our own — leaving two ticks
    both believing they hold the lock, publishing the same posts. With everything
    under one ``BEGIN IMMEDIATE`` the two paths serialize: either we extend first
    and the other tick sees a live lock, or it takes over first and we raise here.
    """
    with db.write_transaction(conn):
        payload = _parse(
            conn.execute("SELECT value FROM meta WHERE key = ?", (LOCK_KEY,)).fetchone()
        )

        if payload is None:
            raise LockError(
                "Не удалось продлить блокировку тика: её больше нет в базе.",
                why="Запись была снята или повреждена, пока тик работал.",
                what_to_do="Дождись следующего тика. Если повторяется — factory doctor.",
            )

        if not _is_ours(payload):
            raise LockError(
                "Не удалось продлить блокировку тика: она не принадлежит этому процессу.",
                why=f"Блокировку держит {payload.get('holder')}:{payload.get('pid')}.",
                what_to_do=(
                    "Скорее всего, предыдущий тик подвис и его блокировку перехватили. "
                    "Этот процесс должен завершиться. См. RUNBOOK.md → «Когда сломалось»."
                ),
            )

        if _is_expired(payload):
            # Still ours, so nobody has taken over yet and extending is safe: any
            # competing tick has to pass through the same write lock we hold here.
            # Worth a warning though — a tick that outruns its TTL is a symptom.
            log.warning(
                "тик работает дольше своего TTL, блокировка продлена",
                extra={"ttl_sec": paths.lock_ttl_sec()},
            )

        _store(conn, _payload(now_utc() + timedelta(seconds=paths.lock_ttl_sec())))


def force_unlock(conn: sqlite3.Connection) -> bool:
    """Remove the lock regardless of who holds it. Returns whether one existed."""
    with db.write_transaction(conn):
        cursor = conn.execute("DELETE FROM meta WHERE key = ?", (LOCK_KEY,))
        return cursor.rowcount > 0


def write_heartbeat(conn: sqlite3.Connection) -> None:
    """Record that a tick finished successfully. Read by the Docker healthcheck."""
    stamp = to_iso(now_utc())
    with db.write_transaction(conn):
        conn.execute(UPSERT_META, (HEARTBEAT_KEY, stamp, stamp))


def heartbeat_age_sec(conn: sqlite3.Connection) -> float | None:
    """Seconds since the last successful tick, or ``None`` if there never was one."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (HEARTBEAT_KEY,)).fetchone()
    if row is None:
        return None
    try:
        return (now_utc() - from_iso(row["value"])).total_seconds()
    except ValueError:
        return None


def heartbeat_is_stale(conn: sqlite3.Connection) -> bool:
    """Whether the owner should be told the worker has stopped moving.

    A system that has never ticked counts as stale: it is just as broken as one
    that stopped two hours ago, and just as worth reporting.
    """
    age = heartbeat_age_sec(conn)
    return age is None or age > HEARTBEAT_STALE_AFTER_SEC
