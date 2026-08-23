"""Блокировка тика и хартбит.

Два требования спорят друг с другом: два тика не должны работать одновременно,
но убитый процесс не должен блокировать систему на полчаса. Отсюда TTL и
перехват протухшей блокировки.
"""

import json
import os
from datetime import timedelta

import pytest

from factory.core import db, lock
from factory.core.clock import from_iso, now_utc, to_iso
from factory.core.errors import LockError


def stored(conn) -> dict:
    row = conn.execute("SELECT value FROM meta WHERE key = 'tick_lock'").fetchone()
    return json.loads(row["value"]) if row else {}


class TestAcquire:
    def test_first_holder_gets_it(self, conn):
        with lock.tick_lock(conn) as held:
            assert held is True
            assert stored(conn)["pid"] == os.getpid()

    def test_lock_is_released_on_exit(self, conn):
        with lock.tick_lock(conn):
            pass

        assert conn.execute("SELECT COUNT(*) FROM meta WHERE key='tick_lock'").fetchone()[0] == 0

    def test_second_holder_is_refused(self, conn):
        other = db.connect()
        try:
            with lock.tick_lock(conn) as first:
                with lock.tick_lock(other) as second:
                    assert first is True
                    assert second is False
        finally:
            other.close()

    def test_refused_holder_does_not_touch_the_stored_lock(self, conn):
        """Проигравший тик обязан уйти, ничего не трогая, — иначе смысл теряется."""
        other = db.connect()
        try:
            with lock.tick_lock(conn):
                owner_before = stored(conn)
                with lock.tick_lock(other) as second:
                    assert second is False
                assert stored(conn) == owner_before
            assert stored(conn) == {}
        finally:
            other.close()

    def test_lock_survives_an_exception_inside_the_block(self, conn):
        with pytest.raises(ValueError):
            with lock.tick_lock(conn):
                raise ValueError("тик упал")

        assert stored(conn) == {}, "блокировка должна сниматься даже при исключении"

    def test_acquired_lock_expires_in_the_future(self, conn, monkeypatch):
        monkeypatch.setenv("FACTORY_LOCK_TTL_SEC", "1800")
        with lock.tick_lock(conn):
            expires = from_iso(stored(conn)["expires_at"])
            assert now_utc() < expires <= now_utc() + timedelta(seconds=1800)


class TestStaleLock:
    def test_expired_lock_is_taken_over(self, conn):
        """После kill -9 запись остаётся висеть. Ждать полчаса нельзя."""
        lock._write(conn, holder="умерший-процесс", pid=999999, expires_at=now_utc() - timedelta(seconds=1))

        with lock.tick_lock(conn) as held:
            assert held is True
            assert stored(conn)["pid"] == os.getpid()

    def test_live_lock_is_not_taken_over(self, conn):
        lock._write(conn, holder="живой", pid=999999, expires_at=now_utc() + timedelta(minutes=10))

        with lock.tick_lock(conn) as held:
            assert held is False

    def test_takeover_uses_the_configured_ttl_for_the_new_lock(self, conn, monkeypatch):
        """Перехваченная блокировка должна получить СВОЙ срок, а не унаследовать чужой.

        Проверяется именно длительность: без этого тест проходил бы при любом
        значении TTL и не проверял ничего.
        """
        monkeypatch.setenv("FACTORY_LOCK_TTL_SEC", "1")
        lock._write(conn, holder="умерший", pid=999999, expires_at=now_utc() - timedelta(hours=5))

        with lock.tick_lock(conn) as held:
            assert held is True
            expires = from_iso(stored(conn)["expires_at"])
            assert now_utc() - timedelta(seconds=2) < expires <= now_utc() + timedelta(seconds=1)

    def test_takeover_does_not_delete_the_lock_of_the_process_that_stole_it(self, conn):
        """Перехватчик обязан снять блокировку за собой, а не оставить висеть."""
        lock._write(conn, holder="умерший", pid=999999, expires_at=now_utc() - timedelta(hours=1))

        with lock.tick_lock(conn):
            pass

        assert stored(conn) == {}

    def test_garbage_in_the_lock_row_does_not_wedge_the_system(self, conn):
        """Битую запись надо перехватывать, а не падать на ней каждые 10 минут."""
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES ('tick_lock', 'не json', ?)",
                (to_iso(now_utc()),),
            )

        with lock.tick_lock(conn) as held:
            assert held is True


class TestRefresh:
    def test_refresh_pushes_the_expiry_forward(self, conn, monkeypatch):
        monkeypatch.setenv("FACTORY_LOCK_TTL_SEC", "60")
        with lock.tick_lock(conn):
            before = from_iso(stored(conn)["expires_at"])
            monkeypatch.setenv("FACTORY_LOCK_TTL_SEC", "600")
            lock.refresh(conn)
            after = from_iso(stored(conn)["expires_at"])

        assert after > before

    def test_refresh_by_a_stranger_is_refused(self, conn):
        """Иначе чужой процесс продлит блокировку, которую сам не держит."""
        lock._write(conn, holder="чужой", pid=999999, expires_at=now_utc() + timedelta(minutes=10))

        with pytest.raises(LockError, match="не принадлежит"):
            lock.refresh(conn)

    def test_refresh_without_a_lock_is_refused(self, conn):
        with pytest.raises(LockError):
            lock.refresh(conn)


class TestForceUnlock:
    def test_removes_a_live_lock(self, conn):
        lock._write(conn, holder="живой", pid=999999, expires_at=now_utc() + timedelta(minutes=10))

        assert lock.force_unlock(conn) is True
        assert stored(conn) == {}

    def test_reports_when_there_was_nothing_to_unlock(self, conn):
        assert lock.force_unlock(conn) is False


class TestHeartbeat:
    def test_written_and_read_back(self, conn):
        lock.write_heartbeat(conn)

        assert lock.heartbeat_age_sec(conn) is not None
        assert lock.heartbeat_age_sec(conn) < 5

    def test_missing_heartbeat_reads_as_none(self, conn):
        assert lock.heartbeat_age_sec(conn) is None

    def test_age_grows_with_time(self, conn):
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES ('heartbeat', ?, ?)",
                (to_iso(now_utc() - timedelta(hours=3)), to_iso(now_utc())),
            )

        age = lock.heartbeat_age_sec(conn)
        assert 3 * 3600 - 5 < age < 3 * 3600 + 5

    def test_is_stale_uses_the_two_hour_threshold_from_the_spec(self, conn):
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES ('heartbeat', ?, ?)",
                (to_iso(now_utc() - timedelta(hours=2, minutes=1)), to_iso(now_utc())),
            )

        assert lock.heartbeat_is_stale(conn) is True

    def test_fresh_heartbeat_is_not_stale(self, conn):
        lock.write_heartbeat(conn)
        assert lock.heartbeat_is_stale(conn) is False

    def test_missing_heartbeat_counts_as_stale(self, conn):
        """Система, ни разу не отработавшая, — тоже повод разбудить владельца."""
        assert lock.heartbeat_is_stale(conn) is True

    def test_writing_twice_updates_rather_than_duplicates(self, conn):
        lock.write_heartbeat(conn)
        lock.write_heartbeat(conn)

        count = conn.execute("SELECT COUNT(*) FROM meta WHERE key = 'heartbeat'").fetchone()[0]
        assert count == 1
