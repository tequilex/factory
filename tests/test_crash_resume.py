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


# Запускаем воркер напрямую, а не через `uv run`.
#
# `uv run` порождает воркер дочерним процессом и остаётся его родителем.
# SIGKILL переслать нельзя, поэтому os.kill по pid от Popen убивает только uv,
# а воркер осиротевает и продолжает тикать. Тесты при этом проходят — но не
# потому, что система возобновляема, а потому, что её никто не убивал; заодно
# каждый прогон оставлял в системе бессмертные процессы.
FACTORY_BIN = REPO_ROOT / ".venv" / "bin" / "factory"


def factory(*args, env, timeout=90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(FACTORY_BIN), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


#: Воркеры, запущенные тестами. Нужны, чтобы прибирать за собой ТОЧЕЧНО.
_STARTED: list[subprocess.Popen] = []


def start_worker(env) -> subprocess.Popen:
    """Запускает воркер в отдельной группе процессов, чтобы его можно было убить."""
    worker = subprocess.Popen(
        [str(FACTORY_BIN), "run", "--loop"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _STARTED.append(worker)
    return worker


def kill_worker(worker: subprocess.Popen) -> None:
    """Убивает всю группу процессов воркера — SIGKILL по одному pid не наследуется."""
    try:
        os.killpg(os.getpgid(worker.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    worker.wait(timeout=30)


@pytest.fixture(autouse=True)
def no_stray_workers():
    """Добивает воркеры, запущенные ЭТИМ тестом. Только их.

    Осиротевший воркер продолжает писать в ту же базу, из-за чего следующий тест
    проверяет не то, что думает, а integrity_check идёт по базе, в которую прямо
    сейчас пишут. Прибирать за собой обязательно.

    Раньше здесь стоял ``pkill -9 -f "factory run --loop"``, и он выкашивал
    ВСЕ воркеры на машине — включая рабочий, запущенный владельцем в соседнем
    терминале. Три раза подряд это выглядело как «воркер молча умирает сам»:
    он падал ровно в момент прогона тестов, а в логе оставался обычный тик без
    единой ошибки. На сервере, где тесты гоняют рядом с боевым процессом, та же
    строка остановила бы выпуск постов.

    Тест не имеет права трогать ничего за пределами того, что сам запустил.
    """
    yield
    for worker in _STARTED:
        try:
            os.killpg(os.getpgid(worker.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    _STARTED.clear()


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

        worker = start_worker(env)
        time.sleep(6)
        kill_worker(worker)

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
        worker = start_worker(env)
        time.sleep(5)
        kill_worker(worker)

        conn = open_worker_db(env)
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()

        assert result == "ok"

    def test_stale_lock_after_a_kill_is_taken_over(self, workspace):
        """Иначе после аварии система стоит до истечения TTL и никто не понимает почему."""
        env = workspace["env"]
        worker = start_worker(env)
        time.sleep(4)
        kill_worker(worker)

        time.sleep(3)
        result = factory("run", "--once", env=env)

        assert result.returncode == 0
        assert "уже работает другой процесс" not in result.stdout

    def test_sigterm_stops_cleanly_and_releases_the_lock(self, workspace):
        """docker stop шлёт SIGTERM: воркер обязан доработать тик и отпустить блокировку."""
        env = workspace["env"]
        worker = start_worker(env)
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

    def test_the_publisher_is_called_once_per_post(self, workspace):
        """Дубль поста в группе недопустим.

        Считаются вызовы публикатора, а не разные external_id: у заглушки
        external_id = stub_{post_id} уникален по построению, поэтому группировка
        по нему была бы истинна при любой реализации — в том числе при снятой
        проверке `external_id IS NULL`. Реальный след публикации — строка в
        `runs` со step='approved' и ok=1, и файл, который пишет заглушка.
        """
        env = workspace["env"]
        for _ in range(10):
            factory("run", "--once", env=env)

        conn = open_worker_db(env)
        published = conn.execute(
            "SELECT id FROM posts WHERE external_id IS NOT NULL ORDER BY id"
        ).fetchall()
        calls = conn.execute(
            "SELECT post_id, COUNT(*) AS n FROM runs WHERE step = 'approved' AND ok = 1 "
            "GROUP BY post_id"
        ).fetchall()
        conn.close()

        assert published, "за десять тиков ничего не опубликовалось"

        by_post = {row["post_id"]: row["n"] for row in calls}
        for row in published:
            assert by_post.get(row["id"], 0) == 1, (
                f"пост {row['id']} прошёл шаг публикации {by_post.get(row['id'])} раз"
            )

    def test_published_at_never_moves(self, workspace):
        """Вторая публикация переписала бы published_at — значит, он и есть свидетель."""
        env = workspace["env"]
        for _ in range(4):
            factory("run", "--once", env=env)

        conn = open_worker_db(env)
        before = {
            row["id"]: row["published_at"]
            for row in conn.execute(
                "SELECT id, published_at FROM posts WHERE published_at IS NOT NULL"
            ).fetchall()
        }
        conn.close()
        assert before, "за четыре тика ничего не опубликовалось"

        for _ in range(6):
            factory("run", "--once", env=env)

        conn = open_worker_db(env)
        after = {
            row["id"]: row["published_at"]
            for row in conn.execute(
                "SELECT id, published_at FROM posts WHERE published_at IS NOT NULL"
            ).fetchall()
        }
        conn.close()

        for post_id, stamp in before.items():
            assert after[post_id] == stamp, f"пост {post_id} опубликован повторно"
