"""Схема базы и применение миграций."""

import sqlite3

import pytest

from factory.core import db, paths
from factory.core.errors import DbError, FactoryError
from tests.conftest import insert_post, insert_project, insert_topic

# Версия поднимается осознанно при каждой новой миграции. Литерал, а не
# вычисление из каталога: иначе тест сравнивал бы схему сам с собой.
EXPECTED_SCHEMA_VERSION = 2

EXPECTED_TABLES = {
    "projects",
    "topics",
    "posts",
    "assets",
    "comments",
    "runs",
    "rejections",
    "meta",
}


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows} - {"sqlite_sequence"}


def test_all_tables_are_created(conn):
    assert EXPECTED_TABLES <= table_names(conn)


def test_schema_version_is_recorded(conn):
    assert db.schema_version(conn) == EXPECTED_SCHEMA_VERSION


def test_migrate_is_idempotent(conn):
    before = table_names(conn)
    assert db.migrate(conn) == EXPECTED_SCHEMA_VERSION
    assert db.migrate(conn) == EXPECTED_SCHEMA_VERSION
    assert table_names(conn) == before


def test_wal_mode_is_on(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_foreign_keys_pragma_is_actually_enabled(conn):
    """SQLite выключает внешние ключи по умолчанию — включение легко потерять.

    Без него осиротевшие строки в assets и runs копились бы молча, а тесты,
    проверяющие связи, продолжали бы проходить.
    """
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_busy_timeout_is_set(conn):
    """Иначе второй писатель падает мгновенно вместо того, чтобы подождать."""
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS


def test_synchronous_is_normal(conn):
    """NORMAL в паре с WAL: безопасно при kill -9 и щадяще к SD-карте."""
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO topics (project_id, title) VALUES (?, ?)",
            (999, "тема без проекта"),
        )


def test_duplicate_idem_key_is_rejected(conn):
    """Дубль поста по одной теме недопустим — это ловится на уровне базы."""
    project_id = insert_project(conn)
    topic_id = insert_topic(conn, project_id)
    insert_post(conn, project_id, topic_id, idem_key="demo:1:0")

    with pytest.raises(sqlite3.IntegrityError):
        insert_post(conn, project_id, topic_id, idem_key="demo:1:0")


def test_same_topic_with_a_new_attempt_is_allowed(conn):
    """Отклонённая тема переиспользуется — третий сегмент ключа это и обеспечивает."""
    project_id = insert_project(conn)
    topic_id = insert_topic(conn, project_id)
    insert_post(conn, project_id, topic_id, idem_key=f"demo:{topic_id}:0")
    insert_post(conn, project_id, topic_id, idem_key=f"demo:{topic_id}:1")

    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2


def test_unknown_state_is_rejected(conn):
    """Опечатка в имени состояния означала бы пост, стоящий навсегда."""
    project_id = insert_project(conn)
    topic_id = insert_topic(conn, project_id)

    with pytest.raises(sqlite3.IntegrityError):
        insert_post(conn, project_id, topic_id, state="text_redy")


def test_unknown_topic_status_is_rejected(conn):
    project_id = insert_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO topics (project_id, title, status) VALUES (?, ?, ?)",
            (project_id, "тема", "taked"),
        )


def test_unknown_asset_kind_is_rejected(conn):
    project_id = insert_project(conn)
    topic_id = insert_topic(conn, project_id)
    post_id = insert_post(conn, project_id, topic_id)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO assets (post_id, kind, position, created_at) VALUES (?, ?, ?, ?)",
            (post_id, "banner", 0, "2026-08-23T10:00:00Z"),
        )


def test_one_cover_per_position(conn):
    """Повторный шаг не должен плодить дубли картинок — страховка на уровне базы."""
    project_id = insert_project(conn)
    topic_id = insert_topic(conn, project_id)
    post_id = insert_post(conn, project_id, topic_id)
    conn.execute(
        "INSERT INTO assets (post_id, kind, position, created_at) VALUES (?, 'cover', 0, ?)",
        (post_id, "2026-08-23T10:00:00Z"),
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO assets (post_id, kind, position, created_at) VALUES (?, 'cover', 0, ?)",
            (post_id, "2026-08-23T10:00:00Z"),
        )


class TestTransactions:
    def test_commit_persists(self, conn):
        with db.write_transaction(conn):
            insert_project(conn, "kept")

        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1

    def test_rollback_on_error_leaves_nothing_behind(self, conn):
        with pytest.raises(ValueError):
            with db.write_transaction(conn):
                insert_project(conn, "discarded")
                raise ValueError("шаг упал")

        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0

    def test_read_transaction_sees_a_consistent_snapshot(self, conn):
        insert_project(conn, "before")
        other = db.connect()
        try:
            with db.read_transaction(conn):
                first = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
                with db.write_transaction(other):
                    insert_project(other, "added-midway")
                second = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        finally:
            other.close()

        assert first == second == 1

    def test_reader_does_not_block_a_writer(self, conn):
        """Иначе на Этапе 6 воркер комментариев встанет в очередь за тиком без причины."""
        other = db.connect()
        other.execute("PRAGMA busy_timeout = 50")
        try:
            with db.read_transaction(conn):
                conn.execute("SELECT COUNT(*) FROM posts").fetchone()
                with db.write_transaction(other):
                    insert_project(other, "written-while-reading")
        finally:
            other.close()

        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1

    def test_writer_blocks_another_writer(self, conn):
        """Два одновременных писателя — то, от чего защищает BEGIN IMMEDIATE."""
        other = db.connect()
        other.execute("PRAGMA busy_timeout = 50")
        try:
            with db.write_transaction(conn):
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()


class TestMigrationDiscovery:
    def test_missing_directory_explains_itself(self, conn, tmp_path):
        with pytest.raises(DbError) as excinfo:
            db.migrate(conn, tmp_path / "нет-такого")

        assert "FACTORY_MIGRATIONS_DIR" in str(excinfo.value)

    def test_badly_named_file_is_refused(self, conn, tmp_path):
        (tmp_path / "init.sql").write_text("SELECT 1;", encoding="utf-8")

        with pytest.raises(DbError) as excinfo:
            db.migrate(conn, tmp_path)

        assert "NNN_описание.sql" in str(excinfo.value)

    def test_duplicate_numbers_are_refused(self, conn, tmp_path):
        (tmp_path / "020_a.sql").write_text("SELECT 1;", encoding="utf-8")
        (tmp_path / "020_b.sql").write_text("SELECT 1;", encoding="utf-8")

        with pytest.raises(DbError, match="одинаковым номером"):
            db.migrate(conn, tmp_path)

    def test_migrations_apply_in_numeric_not_alphabetic_order(self, conn, tmp_path):
        """10 идёт после 9, а не между 1 и 2 — сортировка строк тут врёт."""
        (tmp_path / "020_first.sql").write_text(
            "CREATE TABLE step_two (id INTEGER);", encoding="utf-8"
        )
        (tmp_path / "030_second.sql").write_text(
            "ALTER TABLE step_two ADD COLUMN name TEXT;", encoding="utf-8"
        )

        assert db.migrate(conn, tmp_path) == 30

    def test_failed_migration_leaves_the_previous_version_intact(self, conn, tmp_path):
        (tmp_path / "020_broken.sql").write_text("СИНТАКСИС НЕ SQL;", encoding="utf-8")

        with pytest.raises(DbError):
            db.migrate(conn, tmp_path)

        assert db.schema_version(conn) == EXPECTED_SCHEMA_VERSION
        assert EXPECTED_TABLES <= table_names(conn)


class TestConnect:
    def test_unwritable_data_dir_gives_advice(self, monkeypatch, tmp_path):
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        monkeypatch.setenv("FACTORY_DATA_DIR", str(readonly / "data"))

        try:
            with pytest.raises(FactoryError) as excinfo:
                db.connect()
        finally:
            readonly.chmod(0o700)

        assert "export FACTORY_DATA_DIR=" in str(excinfo.value)

    def test_open_db_connects_and_migrates(self):
        conn = db.open_db()
        try:
            assert db.schema_version(conn) == EXPECTED_SCHEMA_VERSION
            assert paths.db_path().exists()
        finally:
            conn.close()
