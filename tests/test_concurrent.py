"""Параллельные запуски.

Два разных механизма, и проверяются они порознь:

* блокировка тика — второй процесс просто уходит, не начиная работу;
* атомарность захвата темы — на случай, если до захвата всё-таки дошли двое.

Проверять только первое недостаточно: при работающей блокировке второй процесс
до `claim_free_topic` не доходит вовсе, и атомарность самого захвата остаётся
непроверенной. Поэтому здесь она проверяется отдельно, в обход блокировки.
"""

import os
import subprocess
import threading
from pathlib import Path

import pytest

from factory.core import db, machine
from factory.core.config import load_project
from factory.core.models import Project
from tests.conftest import REPO_ROOT, insert_project, insert_topic


class TestTopicClaimRace:
    """Гонка за темами напрямую, минуя блокировку тика."""

    @pytest.fixture
    def five_topics(self, conn, demo_project):
        project_id = insert_project(conn, "demo")
        for i in range(5):
            insert_topic(conn, project_id, f"Тема {i}")
        conn.commit()
        return project_id

    def test_every_topic_goes_to_exactly_one_thread(self, five_topics, tmp_env):
        """Восемь потоков, пять тем: пять успехов, три отказа, ни одного дубля."""
        results: list[int | None] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)
        guard = threading.Lock()

        def claim() -> None:
            connection = db.connect()
            connection.execute("PRAGMA busy_timeout = 5000")
            try:
                barrier.wait(timeout=10)
                topic_id = machine.claim_free_topic(connection, five_topics)
                with guard:
                    results.append(topic_id)
            except BaseException as exc:  # noqa: BLE001 — переносим в главный поток
                with guard:
                    errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, f"потоки упали: {errors}"

        claimed = [topic_id for topic_id in results if topic_id is not None]
        refused = [topic_id for topic_id in results if topic_id is None]

        assert len(claimed) == 5, f"захвачено {len(claimed)} тем вместо пяти"
        assert len(refused) == 3
        assert len(set(claimed)) == 5, "одна тема досталась нескольким потокам"

    def test_no_topic_is_left_free_or_double_taken(self, five_topics, tmp_env):
        barrier = threading.Barrier(8)

        def claim() -> None:
            connection = db.connect()
            connection.execute("PRAGMA busy_timeout = 5000")
            try:
                barrier.wait(timeout=10)
                machine.claim_free_topic(connection, five_topics)
            finally:
                connection.close()

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        conn = db.connect()
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM topics GROUP BY status").fetchall()
        conn.close()

        counts = {row["status"]: row["n"] for row in rows}
        assert counts == {"taken": 5}


class TestPostCreationRace:
    """Тот же забег, но через боевой путь: захват темы плюс создание поста."""

    @pytest.fixture
    def project(self, conn, demo_project):
        project_id = insert_project(conn, "demo")
        for i in range(5):
            insert_topic(conn, project_id, f"Тема {i}")
        conn.commit()
        return Project.from_row(
            conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        )

    def test_no_duplicate_posts_and_no_lost_topics(self, project, tmp_env):
        created: list[int | None] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)
        guard = threading.Lock()

        def make_post() -> None:
            connection = db.connect()
            connection.execute("PRAGMA busy_timeout = 5000")
            try:
                barrier.wait(timeout=10)
                post_id = machine.create_post_for_next_topic(connection, project)
                with guard:
                    created.append(post_id)
            except BaseException as exc:  # noqa: BLE001
                with guard:
                    errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=make_post) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, f"потоки упали: {errors}"

        conn = db.connect()
        posts = conn.execute("SELECT topic_id, idem_key FROM posts").fetchall()
        free = conn.execute("SELECT COUNT(*) FROM topics WHERE status = 'free'").fetchone()[0]
        conn.close()

        assert len(posts) == 5
        assert len({row["topic_id"] for row in posts}) == 5, "по одной теме создано два поста"
        assert len({row["idem_key"] for row in posts}) == 5
        assert free == 0


class TestParallelTicks:
    """Два процесса `factory run --once` одновременно."""

    @pytest.fixture
    def workspace(self, tmp_path, demo_project):
        env = dict(os.environ)
        env.update(
            {
                "FACTORY_DATA_DIR": str(tmp_path / "data"),
                "FACTORY_TMP_DIR": str(tmp_path / "tmp"),
                "FACTORY_PROJECTS_DIR": str(tmp_path / "projects"),
                "FACTORY_IGNORE_SCHEDULE": "1",
                "FACTORY_LOCK_TTL_SEC": "60",
                "PATH": f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}",
            }
        )
        topics = tmp_path / "topics.txt"
        topics.write_text("\n".join(f"Тема {i}" for i in range(20)), encoding="utf-8")

        for args in (["init"], ["project", "add", "demo"], ["topics", "import", "demo", str(topics)]):
            result = subprocess.run(
                ["uv", "run", "factory", *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True
            )
            assert result.returncode == 0, result.stderr
        return env

    def test_only_one_tick_does_the_work(self, workspace):
        processes = [
            subprocess.Popen(
                ["uv", "run", "factory", "run", "--once"],
                cwd=REPO_ROOT,
                env=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for _ in range(2)
        ]
        outputs = [process.communicate(timeout=120)[0] for process in processes]

        assert all(process.returncode == 0 for process in processes)
        skipped = [text for text in outputs if "уже работает другой процесс" in text]
        assert len(skipped) == 1, f"блокировка не сработала, вывод: {outputs}"

    def test_buffer_is_not_exceeded_by_parallel_ticks(self, workspace):
        processes = [
            subprocess.Popen(
                ["uv", "run", "factory", "run", "--once"],
                cwd=REPO_ROOT,
                env=workspace,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(3)
        ]
        for process in processes:
            process.wait(timeout=120)

        import sqlite3

        conn = sqlite3.connect(Path(workspace["FACTORY_DATA_DIR"]) / "factory.db")
        conn.row_factory = sqlite3.Row
        posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        keys = conn.execute("SELECT COUNT(DISTINCT idem_key) FROM posts").fetchone()[0]
        topics_taken = conn.execute(
            "SELECT COUNT(*) FROM topics WHERE status != 'free'"
        ).fetchone()[0]
        conn.close()

        buffer_size = load_project("demo").limits.queue_buffer
        assert posts == buffer_size, f"создано {posts} постов при буфере {buffer_size}"
        assert keys == posts, "есть посты с одинаковым idem_key"
        assert topics_taken == posts, "число занятых тем не совпадает с числом постов"
