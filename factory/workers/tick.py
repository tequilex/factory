"""Worker entry point: one tick, or a loop of them.

The scheduler lives here rather than in cron or a systemd timer so the container
needs nothing but the process itself. ``SIGTERM`` — what ``docker stop`` sends —
finishes the tick in progress and then exits, so a restart never lands in the
middle of a step.
"""

from __future__ import annotations

import signal
import sqlite3
import threading

from factory.core import db, machine, paths
from factory.core.config import load_env_file
from factory.core.logging import get_logger

log = get_logger(__name__)


class Stopper:
    """Turns SIGTERM/SIGINT into a flag the loop checks between ticks."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle)

    def _handle(self, signum, frame) -> None:
        log.info("получен сигнал остановки, закончу текущий тик", extra={"signal": signum})
        self._event.set()

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> None:
        """Sleep, but wake immediately when asked to stop."""
        self._event.wait(seconds)


def run_once(conn: sqlite3.Connection | None = None) -> dict:
    """One pass. Used by ``factory run --once`` and by tests."""
    load_env_file()
    connection = conn or db.open_db()
    try:
        return machine.tick(connection)
    finally:
        if conn is None:
            connection.close()


def run_loop(stopper: Stopper | None = None) -> None:
    """Tick forever, pausing ``FACTORY_TICK_INTERVAL_SEC`` between passes."""
    load_env_file()
    stop = stopper or Stopper()
    stop.install()

    conn = db.open_db()
    interval = paths.tick_interval_sec()
    log.info("воркер запущен", extra={"interval_sec": interval})

    try:
        while not stop.stopped:
            try:
                machine.tick(conn)
            except Exception:  # noqa: BLE001 — one bad tick must not kill the worker
                log.exception("тик завершился с ошибкой, продолжаю по расписанию")

            if stop.stopped:
                break
            stop.wait(paths.tick_interval_sec())
    finally:
        conn.close()
        log.info("воркер остановлен")
