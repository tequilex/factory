"""SQLite connection, schema migrations and explicit transactions.

One writer, tens of rows a day. The database file is the entire state of the
system, which is what makes migrating to another machine a file copy.

Transactions are explicit (``isolation_level=None`` plus :func:`transaction`)
because the state machine has to commit after every single step. An implicit
transaction spanning a whole chain of steps would undo the crash-resumability the
design depends on.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from factory.core import paths
from factory.core.errors import DbError

MIGRATION_NAME_RE = re.compile(r"^(\d+)_.+\.sql$")

BUSY_TIMEOUT_MS = 5000


def connect(path: Path | None = None, *, cross_thread: bool = False) -> sqlite3.Connection:
    """Open the database with the pragmas this project relies on.

    ``cross_thread`` нужен панели и только ей. FastAPI разрешает зависимости и
    выполняет обработчик в РАЗНЫХ потоках пула, а SQLite по умолчанию запрещает
    трогать соединение из чужого потока. Ловится это не тестами, которые ходят
    по одному запросу за раз, а живым экраном: пара параллельных запросов — и
    пятисотая ошибка.

    Само по себе это безопасно: соединение живёт один запрос и в двух потоках
    одновременно не используется — они просто идут друг за другом.
    """
    if path is None:
        target = paths.db_path()
        paths.ensure_data_dir()
    else:
        target = path
        target.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = sqlite3.connect(
            target,
            isolation_level=None,
            timeout=BUSY_TIMEOUT_MS / 1000,
            check_same_thread=not cross_thread,
        )
    except sqlite3.Error as exc:
        raise DbError(
            f"Не удалось открыть базу данных {target}.",
            why=str(exc),
            what_to_do=(
                "Проверь, что каталог существует и доступен на запись: "
                "factory doctor. См. RUNBOOK.md → «Когда сломалось»."
            ),
        ) from exc

    conn.row_factory = sqlite3.Row
    # WAL: readers never block the single writer, and a kill -9 cannot corrupt the
    # file. NORMAL synchronous is the standard companion to WAL and is much kinder
    # to an SD card or USB SSD than FULL.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def _begin(conn: sqlite3.Connection, mode: str) -> Iterator[sqlite3.Connection]:
    conn.execute(f"BEGIN {mode}")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transaction that will write. Takes the write lock upfront.

    ``BEGIN IMMEDIATE`` matters: without it two writers would both start as
    readers and deadlock when they try to upgrade — exactly the race the tick lock
    and the topic claim have to survive.

    Use this only when the block actually writes. In WAL mode a writer does not
    block readers, but it does block other writers, so wrapping a read in here
    would make the comments worker queue behind the main tick for no reason.
    """
    with _begin(conn, "IMMEDIATE") as active:
        yield active


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transaction that only reads: a consistent snapshot across several queries.

    ``BEGIN DEFERRED`` takes no write lock, so this runs in parallel with a writer.
    A single ``SELECT`` needs no transaction at all — the connection is in
    autocommit mode.
    """
    with _begin(conn, "DEFERRED") as active:
        yield active


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _discover_migrations(directory: Path) -> list[tuple[int, Path]]:
    if not directory.is_dir():
        raise DbError(
            f"Каталог с миграциями не найден: {directory}.",
            why="Без файлов миграций схему базы создать нельзя.",
            what_to_do=(
                "Запускай команды из корня проекта либо задай путь явно: "
                "export FACTORY_MIGRATIONS_DIR=/путь/к/migrations"
            ),
        )

    found: list[tuple[int, Path]] = []
    for item in sorted(directory.iterdir()):
        if item.suffix != ".sql":
            continue
        match = MIGRATION_NAME_RE.match(item.name)
        if not match:
            raise DbError(
                f"Файл миграции назван неправильно: {item.name}.",
                why="Ожидается формат NNN_описание.sql, например 002_add_column.sql.",
                what_to_do=f"Переименуй файл в каталоге {directory} и повтори запуск.",
            )
        found.append((int(match.group(1)), item))

    numbers = [number for number, _ in found]
    duplicates = {number for number in numbers if numbers.count(number) > 1}
    if duplicates:
        raise DbError(
            f"Две миграции с одинаковым номером: {sorted(duplicates)}.",
            why="Порядок применения стал бы неопределённым.",
            what_to_do=f"Перенумеруй файлы в каталоге {directory}.",
        )

    return sorted(found)


def _apply_one(conn: sqlite3.Connection, number: int, path: Path) -> None:
    """Run a single migration atomically, with foreign keys temporarily off.

    Changing a ``CHECK`` constraint in SQLite means rebuilding the table: create a
    copy, move the rows, drop the original, rename. With foreign keys enforced,
    dropping ``posts`` would fail because ``assets`` and friends reference it, and
    ``PRAGMA foreign_keys`` is a no-op inside a transaction — so it has to be
    switched off out here, around the transaction rather than inside it.

    ``PRAGMA foreign_key_check`` then runs *before* the commit: a migration that
    orphans rows is rolled back instead of being discovered later in production.
    """
    sql = path.read_text(encoding="utf-8")
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        # executescript commits any pending transaction first, so the BEGIN has to
        # live inside the script. It is left open on purpose: the integrity check
        # below still has to be able to roll everything back.
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};")

        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            conn.execute("ROLLBACK")
            tables = sorted({row[0] for row in violations})
            raise DbError(
                f"Миграция {path.name} нарушила связи между таблицами: {', '.join(tables)}.",
                why=(
                    f"После применения нашлось {len(violations)} строк, ссылающихся "
                    "на несуществующие записи. Изменения отменены."
                ),
                what_to_do=(
                    "База осталась на предыдущей версии и работоспособна. "
                    "Исправь файл миграции. См. CLAUDE.md → «Как менять схему»."
                ),
            )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise DbError(
            f"Миграция {path.name} не применилась.",
            why=str(exc),
            what_to_do=(
                "База осталась на предыдущей версии и работоспособна. "
                "Восстанови файл миграции из репозитория или откатись на "
                "предыдущую версию образа. См. RUNBOOK.md → «Обслуживание»."
            ),
        ) from exc
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> int:
    """Apply pending migrations in order. Returns the resulting schema version.

    Each file runs inside its own transaction together with the ``user_version``
    bump, so an interrupted upgrade leaves the database at a version that actually
    matches its schema.
    """
    target_dir = directory or paths.migrations_dir()
    current = schema_version(conn)

    for number, path in _discover_migrations(target_dir):
        if number <= current:
            continue
        _apply_one(conn, number, path)
        current = number

    return current


def open_db() -> sqlite3.Connection:
    """Connect and bring the schema up to date. The entry point for every command."""
    conn = connect()
    migrate(conn)
    return conn
