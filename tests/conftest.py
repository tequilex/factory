"""Общие фикстуры.

Два предохранителя работают автоматически во всех тестах:

* ``tmp_env`` — ни один тест не может дотянуться до настоящего каталога данных;
* ``no_network`` — ни один тест не может случайно уйти в сеть.

Оба включены через ``autouse``: предохранитель, который надо не забыть подключить,
рано или поздно забудут подключить.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest

from factory.core import db, paths
from factory.core.clock import now_utc, to_iso

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def tmp_env(monkeypatch, tmp_path: Path) -> Path:
    """Изолирует каждый тест: свои каталоги данных, временных файлов и проектов."""
    data = tmp_path / "data"
    tmp = tmp_path / "tmp"
    projects = tmp_path / "projects"
    for directory in (data, tmp, projects):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("FACTORY_DATA_DIR", str(data))
    monkeypatch.setenv("FACTORY_TMP_DIR", str(tmp))
    monkeypatch.setenv("FACTORY_PROJECTS_DIR", str(projects))
    monkeypatch.delenv("FACTORY_IGNORE_SCHEDULE", raising=False)
    monkeypatch.delenv("FACTORY_MAX_STEPS_PER_TICK", raising=False)
    monkeypatch.delenv("FACTORY_TICK_INTERVAL_SEC", raising=False)
    monkeypatch.delenv("FACTORY_LOCK_TTL_SEC", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """Роняет любой реальный HTTP-запрос.

    Подменяется транспорт, а не клиент: так предохранитель ловит вызов независимо
    от того, как был создан ``httpx.Client``. ``httpx.MockTransport`` — отдельный
    класс, поэтому замоканные тесты продолжают работать.
    """
    if request.node.get_closest_marker("allow_network"):
        return

    def blocked(self, request: httpx.Request, *args, **kwargs):
        raise RuntimeError(
            f"Тест попытался сходить в сеть: {request.method} {request.url}. "
            "Внешние вызовы в тестах должны быть замоканы. "
            "Если сеть действительно нужна, пометь тест @pytest.mark.allow_network."
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)


@pytest.fixture
def demo_project(tmp_env) -> Path:
    """Копирует настоящий projects/demo во временный каталог проектов.

    Тесты работают с боевым конфигом, но в изоляции: правка теста не может
    испортить файлы в репозитории.
    """
    target = paths.projects_dir() / "demo"
    shutil.copytree(REPO_ROOT / "projects" / "demo", target)
    return target


@pytest.fixture
def conn(tmp_env) -> sqlite3.Connection:
    """Открытая база с применёнными миграциями."""
    connection = db.connect()
    db.migrate(connection)
    yield connection
    connection.close()


def insert_project(conn: sqlite3.Connection, slug: str = "demo") -> int:
    """Заводит проект и возвращает его id."""
    cursor = conn.execute(
        "INSERT INTO projects (slug, config_path, is_active, created_at) VALUES (?, ?, 1, ?)",
        (slug, f"projects/{slug}/config.yaml", to_iso(now_utc())),
    )
    return int(cursor.lastrowid)


def insert_topic(conn: sqlite3.Connection, project_id: int, title: str = "Тема") -> int:
    """Заводит свободную тему и возвращает её id."""
    cursor = conn.execute(
        "INSERT INTO topics (project_id, title, status) VALUES (?, ?, 'free')",
        (project_id, title),
    )
    return int(cursor.lastrowid)


def insert_post(
    conn: sqlite3.Connection,
    project_id: int,
    topic_id: int,
    *,
    state: str = "queued",
    idem_key: str | None = None,
) -> int:
    """Заводит пост и возвращает его id."""
    stamp = to_iso(now_utc())
    cursor = conn.execute(
        """
        INSERT INTO posts (project_id, topic_id, idem_key, state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, topic_id, idem_key or f"demo:{topic_id}:0", state, stamp, stamp),
    )
    return int(cursor.lastrowid)


@pytest.fixture
def seeded(conn: sqlite3.Connection):
    """База с одним проектом и десятью свободными темами."""
    project_id = insert_project(conn)
    topic_ids = [insert_topic(conn, project_id, f"Тема {i}") for i in range(1, 11)]
    conn.commit()
    return {"conn": conn, "project_id": project_id, "topic_ids": topic_ids}
