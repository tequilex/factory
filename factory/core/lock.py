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


def _identity() -> tuple[str, int]:
    return socket.gethostname(), os.getpid()


def _write(conn: sqlite3.Connection, *, holder: str, pid: int, expires_at: datetime) -> None:
    payload = json.dumps(
        {"holder": holder, "pid": pid, "expires_at": to_iso(expires_at)},
        ensure_ascii=False,
    )
    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (LOCK_KEY, payload, to_iso(now_utc())),
        )


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
    holder, pid = _identity()
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

        conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (
                LOCK_KEY,
                json.dumps(
                    {"holder": holder, "pid": pid, "expires_at": to_iso(expires_at)},
                    ensure_ascii=False,
                ),
                to_iso(now_utc()),
            ),
        )
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
    holder, pid = _identity()
    with db.write_transaction(conn):
        payload = _parse(
            conn.execute("SELECT value FROM meta WHERE key = ?", (LOCK_KEY,)).fetchone()
        )
        if payload is None:
            return
        if payload.get("holder") == holder and payload.get("pid") == pid:
            conn.execute("DELETE FROM meta WHERE key = ?", (LOCK_KEY,))


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
    """
    holder, pid = _identity()
    payload = _read(conn)

    if payload is None:
        raise LockError(
            "Не удалось продлить блокировку тика: её больше нет в базе.",
            why="Запись была снята или повреждена, пока тик работал.",
            what_to_do="Дождись следующего тика. Если повторяется — factory doctor.",
        )
    if payload.get("holder") != holder or payload.get("pid") != pid:
        raise LockError(
            "Не удалось продлить блокировку тика: она не принадлежит этому процессу.",
            why=f"Блокировку держит {payload.get('holder')}:{payload.get('pid')}.",
            what_to_do=(
                "Скорее всего, предыдущий тик подвис и его блокировку перехватили. "
                "Этот процесс должен завершиться. См. RUNBOOK.md → «Когда сломалось»."
            ),
        )

    _write(
        conn,
        holder=holder,
        pid=pid,
        expires_at=now_utc() + timedelta(seconds=paths.lock_ttl_sec()),
    )


def force_unlock(conn: sqlite3.Connection) -> bool:
    """Remove the lock regardless of who holds it. Returns whether one existed."""
    with db.write_transaction(conn):
        cursor = conn.execute("DELETE FROM meta WHERE key = ?", (LOCK_KEY,))
        return cursor.rowcount > 0


def write_heartbeat(conn: sqlite3.Connection) -> None:
    """Record that a tick finished successfully. Read by the Docker healthcheck."""
    stamp = to_iso(now_utc())
    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (HEARTBEAT_KEY, stamp, stamp),
        )


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
