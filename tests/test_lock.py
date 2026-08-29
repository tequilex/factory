"""Блокировка тика и хартбит.

Два требования спорят друг с другом: два тика не должны работать одновременно,
но убитый процесс не должен блокировать систему на полчаса. Отсюда TTL и
перехват протухшей блокировки.
"""

import json
import os
from datetime import timedelta

import pytest

from factory.core import db, lock, paths
from factory.core.clock import from_iso, now_utc, to_iso
from factory.core.errors import LockError


def stored(conn) -> dict:
    row = conn.execute("SELECT value FROM meta WHERE key = 'tick_lock'").fetchone()
    return json.loads(row["value"]) if row else {}


def put_lock(conn, *, expires_at, holder="чужой-хост", pid=999999, token="чужой-токен") -> dict:
    """Кладёт в базу блокировку от имени другого процесса."""
    payload = {"holder": holder, "pid": pid, "token": token, "expires_at": to_iso(expires_at)}
    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES ('tick_lock', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(payload, ensure_ascii=False), to_iso(now_utc())),
        )
    return payload


def impersonate_another_process(monkeypatch) -> None:
    """Делает текущий процесс «чужим» для проверок владельца.

    В Docker воркер всегда PID 1, а имя контейнера не меняется при перезапуске,
    поэтому пары (хост, pid) для опознания процесса недостаточно — отсюда токен.
    """
    monkeypatch.setattr(lock, "_PROCESS_TOKEN", "другой-токен")


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

    def test_lock_is_released_even_when_the_block_raises(self, conn):
        with pytest.raises(ValueError):
            with lock.tick_lock(conn):
                raise ValueError("тик упал")

        assert stored(conn) == {}, "блокировка должна сниматься даже при исключении"

    def test_expiry_is_taken_from_the_configured_ttl(self, conn, monkeypatch):
        """Значение нарочно не совпадает с умолчанием, иначе тест ничего не проверяет."""
        monkeypatch.setenv("FACTORY_LOCK_TTL_SEC", "900")
        with lock.tick_lock(conn):
            expires = from_iso(stored(conn)["expires_at"])
            assert now_utc() + timedelta(seconds=880) < expires <= now_utc() + timedelta(seconds=900)

    def test_release_leaves_alone_a_lock_that_is_no_longer_ours(self, conn, monkeypatch):
        """Наш тик подвис, блокировку перехватили, и только потом мы дошли до выхода.

        Снять её означало бы пустить третий тик рядом со вторым. В Docker воркер
        всегда PID 1 на неизменном хосте, поэтому опознание идёт по токену
        процесса — без него эта проверка в бою была бы безусловно истинной.
        """
        with lock.tick_lock(conn):
            impersonate_another_process(monkeypatch)
            taken_over = put_lock(conn, expires_at=now_utc() + timedelta(minutes=10))

        assert stored(conn) == taken_over


class TestStaleLock:
    def test_expired_lock_is_taken_over(self, conn):
        """После kill -9 запись остаётся висеть. Ждать полчаса нельзя."""
        put_lock(conn, holder="умерший-процесс", pid=999999, expires_at=now_utc() - timedelta(seconds=1))

        with lock.tick_lock(conn) as held:
            assert held is True
            assert stored(conn)["pid"] == os.getpid()

    def test_live_lock_is_not_taken_over(self, conn):
        put_lock(conn, holder="живой", pid=999999, expires_at=now_utc() + timedelta(minutes=10))

        with lock.tick_lock(conn) as held:
            assert held is False

    def test_takeover_uses_the_configured_ttl_for_the_new_lock(self, conn, monkeypatch):
        """Перехваченная блокировка должна получить СВОЙ срок, а не унаследовать чужой.

        Проверяется именно длительность: без этого тест проходил бы при любом
        значении TTL и не проверял ничего.
        """
        monkeypatch.setenv("FACTORY_LOCK_TTL_SEC", "1")
        put_lock(conn, holder="умерший", pid=999999, expires_at=now_utc() - timedelta(hours=5))

        with lock.tick_lock(conn) as held:
            assert held is True
            expires = from_iso(stored(conn)["expires_at"])
            assert now_utc() - timedelta(seconds=2) < expires <= now_utc() + timedelta(seconds=1)

    def test_process_that_took_over_releases_the_lock_afterwards(self, conn):
        """Перехватчик обязан снять блокировку за собой, а не оставить висеть."""
        put_lock(conn, holder="умерший", pid=999999, expires_at=now_utc() - timedelta(hours=1))

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
        put_lock(conn, holder="чужой", pid=999999, expires_at=now_utc() + timedelta(minutes=10))

        with pytest.raises(LockError, match="не принадлежит"):
            lock.refresh(conn)

    def test_refresh_without_a_lock_is_refused(self, conn):
        with pytest.raises(LockError):
            lock.refresh(conn)

    def test_refresh_does_not_resurrect_a_lock_that_was_taken_over(self, conn, monkeypatch):
        """Регрессия на гонку: два тика одновременно считали, что держат блокировку.

        Раньше refresh() читал запись, проверял владельца и писал в трёх разных
        транзакциях. За время между чтением и записью наша блокировка могла
        протухнуть, другой тик — перехватить её и начать работу, а мы затирали
        его запись своей. Дальше оба тика брали одни и те же темы и публиковали
        одни и те же посты — ровно то, что SPEC.md называет недопустимым.
        """
        with lock.tick_lock(conn):
            impersonate_another_process(monkeypatch)
            stolen = put_lock(conn, expires_at=now_utc() + timedelta(minutes=10))

            with pytest.raises(LockError, match="не принадлежит"):
                lock.refresh(conn)

            assert stored(conn) == stolen, "refresh затёр блокировку перехватившего тика"

    def test_refresh_extends_our_own_lock_even_after_its_ttl_ran_out(self, conn):
        """Тик может не уложиться в TTL. Пока блокировку не перехватили — продлеваем.

        Это безопасно: перехват идёт через тот же BEGIN IMMEDIATE, поэтому оба
        пути выстраиваются в очередь и одновременно выиграть не могут.
        """
        with lock.tick_lock(conn):
            expired = {**stored(conn), "expires_at": to_iso(now_utc() - timedelta(minutes=5))}
            with db.write_transaction(conn):
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'tick_lock'",
                    (json.dumps(expired, ensure_ascii=False),),
                )

            lock.refresh(conn)

            assert from_iso(stored(conn)["expires_at"]) > now_utc()


class TestForceUnlock:
    def test_removes_a_live_lock(self, conn):
        put_lock(conn, holder="живой", pid=999999, expires_at=now_utc() + timedelta(minutes=10))

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

    def test_just_under_the_threshold_is_not_yet_stale(self, conn):
        """Парный к тесту ниже: только пара 9:59 / 10:01 фиксирует сам порог.

        По отдельности каждый из них проходит при любом пороге от нуля до
        десяти минут, то есть не проверяет ничего.
        """
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES ('heartbeat', ?, ?)",
                (to_iso(now_utc() - timedelta(minutes=9, seconds=59)), to_iso(now_utc())),
            )

        assert lock.heartbeat_is_stale(conn) is False

    def test_past_the_threshold_it_is_stale(self, conn):
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES ('heartbeat', ?, ?)",
                (to_iso(now_utc() - timedelta(minutes=10, seconds=1)), to_iso(now_utc())),
            )

        assert lock.heartbeat_is_stale(conn) is True

    def test_the_default_is_ten_minutes(self):
        """Число литералом: сверять константу с самой собой — не проверка.

        Десять минут — это десять пропущенных тиков при обычной минутной
        частоте. Двухчасовой порог из первой редакции спеки означал сто
        двадцать, и владелец узнавал о вставшем воркере раньше и хуже: нажимал
        кнопку и не получал результата.
        """
        assert paths.heartbeat_stale_sec() == 600

    def test_the_threshold_can_be_changed_from_the_environment(self, monkeypatch, conn):
        """На малине тик реже, а на отладке чаще — порог обязан подстраиваться."""
        monkeypatch.setenv("FACTORY_HEARTBEAT_STALE_SEC", "60")
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES ('heartbeat', ?, ?)",
                (to_iso(now_utc() - timedelta(minutes=2)), to_iso(now_utc())),
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


class TestDeadHolder:
    """Замок, оставшийся от убитого воркера, не должен держать систему полчаса.

    Поймано живьём: тесты убили воркер, он не успел снять замок, и следующий
    полчаса пропускал каждый тик. Посты стояли, картинки не дорисовывались, а в
    логе была одна строка «работает другой процесс» — со стороны неотличимо от
    зависшей системы.
    """

    def _put_lock(self, conn, *, pid, holder=None, minutes=30):
        import json
        import socket

        from factory.core.clock import now_utc, to_iso

        payload = json.dumps({
            "holder": holder or socket.gethostname(),
            "pid": pid,
            "token": "чужой",
            "expires_at": to_iso(now_utc() + timedelta(minutes=minutes)),
        })
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (lock.LOCK_KEY, payload, to_iso(now_utc())),
            )

    def test_a_lock_of_a_dead_process_is_taken(self, conn):
        # Номер, которого заведомо нет: своих детей у теста столько не бывает.
        self._put_lock(conn, pid=999_999)

        with lock.tick_lock(conn) as acquired:
            assert acquired is True

    def test_a_lock_of_a_living_process_is_respected(self, conn):
        """Иначе два воркера пойдут одновременно, и защита от дублей отключится."""
        import os

        self._put_lock(conn, pid=os.getpid())

        with lock.tick_lock(conn) as acquired:
            assert acquired is False

    def test_a_lock_from_another_machine_waits_out_its_term(self, conn):
        """Номера процессов на чужой машине ничего не значат."""
        self._put_lock(conn, pid=999_999, holder="другая-машина")

        with lock.tick_lock(conn) as acquired:
            assert acquired is False
