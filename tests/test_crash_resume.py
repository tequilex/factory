"""Возобновляемость после kill -9.

Приёмочный тест этапа: процесс убивают в произвольный момент, и следующий запуск
продолжает с того места, где всё остановилось. Ничего оплаченного не делается
заново.

Тесты запускают настоящий подпроцесс и настоящий SIGKILL — из внутрипроцессного
мока не видно, переживает ли данные сама запись на диск.
"""

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from factory.core import db, paths
from factory.core.models import State
from tests.conftest import REPO_ROOT


def worker_env(tmp_path: Path, **extra) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "FACTORY_DATA_DIR": str(tmp_path / "data"),
            "FACTORY_TMP_DIR": str(tmp_path / "tmp"),
            "FACTORY_PROJECTS_DIR": str(tmp_path / "projects"),
            "FACTORY_IGNORE_SCHEDULE": "1",
            "FACTORY_TICK_INTERVAL_SEC": "1",
            "FACTORY_MAX_STEPS_PER_TICK": "3",
            "FACTORY_LOCK_TTL_SEC": "2",
            "PATH": f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}",
        }
    )
    env.update({key: str(value) for key, value in extra.items()})
    return env


def factory(*args, env, timeout=90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "factory", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def open_worker_db(env) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(env["FACTORY_DATA_DIR"]) / "factory.db")
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def workspace(tmp_path, demo_project):
    """Каталог с проектом demo, готовый для запуска подпроцессов.

    Проект уже скопирован фикстурой demo_project — сюда он попадает через
    FACTORY_PROJECTS_DIR, который подпроцессы получают в окружении.
    """
    topics = tmp_path / "topics.txt"
    topics.write_text("\n".join(f"Тема номер {i}" for i in range(20)), encoding="utf-8")

    env = worker_env(tmp_path)
    assert factory("init", env=env).returncode == 0
    assert factory("project", "add", "demo", env=env).returncode == 0
    assert factory("topics", "import", "demo", str(topics), env=env).returncode == 0
    return {"tmp_path": tmp_path, "env": env, "topics": topics}


def states(env) -> dict[int, str]:
    conn = open_worker_db(env)
    rows = conn.execute("SELECT id, state FROM posts ORDER BY id").fetchall()
    conn.close()
    return {row["id"]: row["state"] for row in rows}


class TestKillMinusNine:
    def test_progress_survives_and_the_next_run_continues(self, workspace):
        """Убиваем воркер на ходу; следующий запуск доводит посты до конца."""
        env = workspace["env"]
        factory("run", "--once", env=env)
        before = states(env)
        assert before, "первый тик не создал ни одного поста"
        assert all(state != State.QUEUED for state in before.values())

        worker = subprocess.Popen(
            ["uv", "run", "factory", "run", "--loop"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(6)
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=30)

        assert worker.returncode == -signal.SIGKILL

        killed_at = states(env)
        assert killed_at, "после убийства в базе не осталось постов"

        # Блокировка убитого процесса протухает за FACTORY_LOCK_TTL_SEC.
        time.sleep(3)
        for _ in range(12):
            factory("run", "--once", env=env)

        final = states(env)
        assert any(state == State.PUBLISHED for state in final.values()), (
            f"после перезапуска ни один пост не доехал: {final}"
        )

    def test_database_is_not_corrupted_by_the_kill(self, workspace):
        """WAL обязан пережить SIGKILL — иначе теряется вся идея лёгкой миграции."""
        env = workspace["env"]
        worker = subprocess.Popen(
            ["uv", "run", "factory", "run", "--loop"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(5)
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=30)

        conn = open_worker_db(env)
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()

        assert result == "ok"

    def test_stale_lock_after_a_kill_is_taken_over(self, workspace):
        """Иначе после аварии система стоит до истечения TTL и никто не понимает почему."""
        env = workspace["env"]
        worker = subprocess.Popen(
            ["uv", "run", "factory", "run", "--loop"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(4)
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=30)

        time.sleep(3)
        result = factory("run", "--once", env=env)

        assert result.returncode == 0
        assert "уже работает другой процесс" not in result.stdout

    def test_sigterm_stops_cleanly_and_releases_the_lock(self, workspace):
        """docker stop шлёт SIGTERM: воркер обязан доработать тик и отпустить блокировку."""
        env = workspace["env"]
        worker = subprocess.Popen(
            ["uv", "run", "factory", "run", "--loop"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(5)
        worker.terminate()
        worker.wait(timeout=60)

        conn = open_worker_db(env)
        held = conn.execute("SELECT COUNT(*) FROM meta WHERE key = 'tick_lock'").fetchone()[0]
        conn.close()

        assert held == 0, "после штатной остановки блокировка осталась висеть"


class TestPaidWorkIsNotRepeated:
    def test_images_are_not_regenerated_after_a_crash(self, workspace):
        """Сгенерированные картинки оплачены. Перезапуск не должен покупать их снова."""
        env = workspace["env"]
        for _ in range(3):
            factory("run", "--once", env=env)

        # Только посты в работе: у опубликованных файлы удалены намеренно.
        conn = open_worker_db(env)
        rows = conn.execute(
            "SELECT a.id, a.local_path FROM assets a JOIN posts p ON p.id = a.post_id "
            "WHERE a.local_path IS NOT NULL AND p.state != 'published'"
        ).fetchall()
        conn.close()
        assert rows, "за три тика не сгенерировалось ни одной картинки у неопубликованных постов"

        stamps = {row["id"]: Path(row["local_path"]).stat().st_mtime_ns for row in rows}
        time.sleep(0.05)

        for _ in range(3):
            factory("run", "--once", env=env)

        checked = 0
        for asset_id, stamp in stamps.items():
            conn = open_worker_db(env)
            row = conn.execute(
                "SELECT a.local_path, p.state FROM assets a JOIN posts p ON p.id = a.post_id "
                "WHERE a.id = ?",
                (asset_id,),
            ).fetchone()
            conn.close()

            if row["state"] == "published":
                continue  # опубликовался за это время, файлы убраны — так и задумано
            assert Path(row["local_path"]).stat().st_mtime_ns == stamp, (
                f"картинка {asset_id} перегенерирована после перезапуска"
            )
            checked += 1

        assert checked, "все посты успели опубликоваться, проверять было нечего"

    def test_a_published_post_is_never_published_twice(self, workspace):
        """Дубль поста в группе недопустим — проверяем на многих прогонах."""
        env = workspace["env"]
        for _ in range(10):
            factory("run", "--once", env=env)

        conn = open_worker_db(env)
        published = conn.execute(
            "SELECT external_id, COUNT(*) AS n FROM posts WHERE external_id IS NOT NULL "
            "GROUP BY external_id"
        ).fetchall()
        conn.close()

        assert published, "за десять тиков ничего не опубликовалось"
        assert all(row["n"] == 1 for row in published), "один и тот же пост опубликован дважды"
