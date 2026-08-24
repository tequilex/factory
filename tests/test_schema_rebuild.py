"""Проверка механизма миграций на самом дорогом случае — смене CHECK-ограничения.

CHECK на `posts.state` защищает от опечатки в имени состояния, но менять его в
SQLite можно только пересборкой таблицы: создать копию, перелить строки, удалить
оригинал, переименовать. По пути есть две мины:

* внешние ключи не дадут удалить `posts`, на которую ссылаются `assets` и другие;
* `PRAGMA foreign_keys` не действует внутри транзакции, поэтому выключать их
  надо снаружи — а весь механизм миграций как раз оборачивает файл в транзакцию.

Миграции понадобятся уже на Этапах 2–4, и выяснять это на боевой базе с постами
будет поздно. Сама тестовая миграция в `migrations/` не кладётся: проверяется
механизм, а не конкретное изменение схемы.
"""

import sqlite3

import pytest

from factory.core import db
from factory.core.errors import DbError
from tests.conftest import insert_post, insert_project, insert_topic

# Пересборка posts с расширенным списком состояний.
#
# Шаблон для копирования лежит в CLAUDE.md → «Как менять схему базы», вместе с
# описанием трёх граблей. Здесь — его исполняемая копия: если шаблон перестанет
# работать, это упадёт тестом, а не на боевой базе.
REBUILD_POSTS_WITH_NEW_STATE = """
CREATE TABLE posts_new (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    topic_id      INTEGER NOT NULL REFERENCES topics(id),
    idem_key      TEXT UNIQUE NOT NULL,
    state         TEXT NOT NULL DEFAULT 'queued'
                  CHECK (state IN (
                      'queued', 'text_ready', 'factchecked', 'prompts_ready',
                      'images_ready', 'composed', 'in_review', 'approved',
                      'published', 'failed', 'rejected',
                      'review_sent'
                  )),
    title         TEXT,
    body          TEXT,
    question      TEXT,
    factcheck_verdict TEXT,
    factcheck_notes TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    next_attempt_at TEXT,
    scheduled_at  TEXT,
    external_id   TEXT,
    published_at  TEXT,
    publish_guid  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

INSERT INTO posts_new SELECT
    id, project_id, topic_id, idem_key, state, title, body, question,
    factcheck_verdict, factcheck_notes, retry_count, last_error, next_attempt_at,
    scheduled_at, external_id, published_at, publish_guid, created_at, updated_at
FROM posts;

DROP TABLE posts;
ALTER TABLE posts_new RENAME TO posts;

CREATE INDEX idx_posts_active ON posts(state, next_attempt_at);
CREATE INDEX idx_posts_published ON posts(project_id, published_at);
"""


@pytest.fixture
def populated(conn):
    """База версии 1 с постом и всеми зависящими от него строками."""
    project_id = insert_project(conn)
    topic_id = insert_topic(conn, project_id, "Тема с данными")
    post_id = insert_post(conn, project_id, topic_id, state="composed")
    conn.execute(
        "UPDATE posts SET title = ?, body = ?, retry_count = 2 WHERE id = ?",
        ("Заголовок для обложки", "Текст поста", post_id),
    )
    conn.execute(
        "INSERT INTO assets (post_id, kind, position, prompt, created_at) "
        "VALUES (?, 'cover', 0, 'a portrait', ?)",
        (post_id, "2026-08-23T10:00:00Z"),
    )
    conn.execute(
        "INSERT INTO runs (post_id, step, ok, created_at) VALUES (?, 'composed', 1, ?)",
        (post_id, "2026-08-23T10:00:00Z"),
    )
    conn.execute(
        "INSERT INTO rejections (post_id, reason, created_at) VALUES (?, 'images', ?)",
        (post_id, "2026-08-23T10:00:00Z"),
    )
    conn.commit()
    return {"conn": conn, "post_id": post_id, "project_id": project_id, "topic_id": topic_id}


@pytest.fixture
def rebuild_migration(tmp_path):
    """Каталог с единственной миграцией 002 — пересборкой posts."""
    (tmp_path / "020_extend_state_check.sql").write_text(
        REBUILD_POSTS_WITH_NEW_STATE, encoding="utf-8"
    )
    return tmp_path


def test_table_rebuild_applies(populated, rebuild_migration):
    assert db.migrate(populated["conn"], rebuild_migration) == 20
    assert db.schema_version(populated["conn"]) == 20


def test_rows_survive_the_rebuild(populated, rebuild_migration):
    conn = populated["conn"]
    db.migrate(conn, rebuild_migration)

    row = conn.execute("SELECT * FROM posts WHERE id = ?", (populated["post_id"],)).fetchone()
    assert row["state"] == "composed"
    assert row["title"] == "Заголовок для обложки"
    assert row["body"] == "Текст поста"
    assert row["retry_count"] == 2
    assert row["idem_key"] == f"demo:{populated['topic_id']}:0"


def test_dependent_rows_survive_and_still_resolve(populated, rebuild_migration):
    """Строки в assets/runs/rejections не должны осиротеть после DROP TABLE posts."""
    conn = populated["conn"]
    db.migrate(conn, rebuild_migration)

    for table in ("assets", "runs", "rejections"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 1, f"строка из {table} потерялась при пересборке"

    joined = conn.execute(
        "SELECT p.title FROM assets a JOIN posts p ON p.id = a.post_id"
    ).fetchone()
    assert joined["title"] == "Заголовок для обложки"


def test_foreign_keys_are_back_on_after_the_migration(populated, rebuild_migration):
    conn = populated["conn"]
    db.migrate(conn, rebuild_migration)

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO assets (post_id, kind, position, created_at) VALUES (999, 'cover', 0, ?)",
            ("2026-08-23T10:00:00Z",),
        )


def test_new_state_is_accepted_and_old_check_is_gone(populated, rebuild_migration):
    conn = populated["conn"]
    db.migrate(conn, rebuild_migration)

    conn.execute("UPDATE posts SET state = 'review_sent' WHERE id = ?", (populated["post_id"],))
    assert conn.execute("SELECT state FROM posts").fetchone()[0] == "review_sent"


def test_check_still_rejects_typos_after_the_rebuild(populated, rebuild_migration):
    conn = populated["conn"]
    db.migrate(conn, rebuild_migration)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE posts SET state = 'text_redy' WHERE id = ?", (populated["post_id"],))


def test_indexes_are_recreated(populated, rebuild_migration):
    """Индексы уходят вместе с таблицей — забыть их пересоздать легко."""
    conn = populated["conn"]
    db.migrate(conn, rebuild_migration)

    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'posts'"
    ).fetchall()
    names = {row["name"] for row in rows}
    assert {"idx_posts_active", "idx_posts_published"} <= names


def test_migration_that_orphans_rows_is_rolled_back(populated, tmp_path):
    """Забыли перелить строки — миграция должна откатиться, а не оставить мусор."""
    (tmp_path / "020_forgot_to_copy.sql").write_text(
        """
        CREATE TABLE posts_new (
            id            INTEGER PRIMARY KEY,
            project_id    INTEGER NOT NULL REFERENCES projects(id),
            topic_id      INTEGER NOT NULL REFERENCES topics(id),
            idem_key      TEXT UNIQUE NOT NULL,
            state         TEXT NOT NULL DEFAULT 'queued',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
        DROP TABLE posts;
        ALTER TABLE posts_new RENAME TO posts;
        """,
        encoding="utf-8",
    )
    conn = populated["conn"]

    with pytest.raises(DbError) as excinfo:
        db.migrate(conn, tmp_path)

    assert "нарушила связи" in str(excinfo.value)
    assert db.schema_version(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
