"""CLI.

Это один из двух интерфейсов, которыми пользуется владелец (второй — Telegram).
Поэтому проверяется не только «команда отработала», но и что она сказала: код
возврата, понятный текст, отсутствие трейсбеков.
"""

import pytest
from typer.testing import CliRunner

from factory.cli import app
from factory.core import db, paths
from factory.core.clock import now_utc, to_iso
from factory.core.models import State
from tests.conftest import insert_post, insert_project, insert_topic

runner = CliRunner()


def run(*args) -> object:
    return runner.invoke(app, list(args), catch_exceptions=False)


def assert_no_traceback(result) -> None:
    assert "Traceback" not in result.output
    assert "Error:" not in result.output or "FactoryError" not in result.output


@pytest.fixture
def topics_file(tmp_path):
    path = tmp_path / "topics.txt"
    path.write_text(
        "Как выбрать шины на зиму\n"
        "\n"
        "Почему греется двигатель\n"
        "Как выбрать шины на зиму\n"
        "   \n"
        "Что делать при проколе\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def ready(tmp_env, demo_project, topics_file):
    """База создана, проект demo подключён, темы загружены."""
    run("init")
    run("project", "add", "demo")
    run("topics", "import", "demo", str(topics_file))
    return {"topics_file": topics_file}


class TestInit:
    def test_creates_the_database(self, tmp_env, demo_project):
        result = run("init")

        assert result.exit_code == 0
        assert paths.db_path().exists()
        assert "версия схемы 1" in result.output

    def test_running_twice_is_safe(self, tmp_env, demo_project):
        run("init")
        result = run("init")

        assert result.exit_code == 0

    def test_unwritable_data_dir_gives_advice(self, tmp_env, monkeypatch, tmp_path):
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        monkeypatch.setenv("FACTORY_DATA_DIR", str(readonly / "data"))

        try:
            result = run("init")
        finally:
            readonly.chmod(0o700)

        assert result.exit_code == 1
        assert "export FACTORY_DATA_DIR=" in result.output
        assert_no_traceback(result)


class TestProject:
    def test_add_registers_the_project(self, tmp_env, demo_project):
        run("init")
        result = run("project", "add", "demo")

        assert result.exit_code == 0
        assert "подключён" in result.output
        assert "Публикаций в сутки: 2" in result.output

    def test_add_twice_says_so_without_failing(self, tmp_env, demo_project):
        run("init")
        run("project", "add", "demo")
        result = run("project", "add", "demo")

        assert result.exit_code == 0
        assert "уже подключён" in result.output
        assert_no_traceback(result)

    def test_add_unknown_project_lists_what_exists(self, tmp_env, demo_project):
        run("init")
        result = run("project", "add", "auto_girl")

        assert result.exit_code == 1
        assert "demo" in result.output
        assert_no_traceback(result)

    def test_list_shows_free_topics(self, ready):
        result = run("project", "list")

        assert "demo" in result.output
        assert "свободных тем: 3" in result.output

    def test_pause_and_resume(self, ready):
        paused = run("project", "pause", "demo")
        assert "на паузе" in paused.output
        assert "на паузе" in run("project", "list").output

        run("project", "resume", "demo")
        assert "включён" in run("project", "list").output

    def test_pause_of_unknown_project_is_explained(self, ready):
        result = run("project", "pause", "нет_такого")

        assert result.exit_code == 1
        assert "factory project add" in result.output


class TestTopics:
    def test_import_counts_added_and_skipped(self, tmp_env, demo_project, topics_file):
        run("init")
        run("project", "add", "demo")

        result = run("topics", "import", "demo", str(topics_file))

        assert result.exit_code == 0
        assert "Загружено тем: 3" in result.output
        assert "Пропущено (пустые и повторы): 3" in result.output

    def test_reimport_adds_nothing_new(self, ready):
        result = run("topics", "import", "demo", str(ready["topics_file"]))

        assert "Загружено тем: 0" in result.output

    def test_missing_file_is_explained(self, ready):
        result = run("topics", "import", "demo", "/нет/такого/файла.txt")

        assert result.exit_code == 1
        assert "одна тема на строку" in result.output
        assert_no_traceback(result)

    def test_list_shows_the_breakdown(self, ready):
        result = run("topics", "list", "demo")

        assert "свободных: 3" in result.output

    def test_list_warns_when_topics_run_out(self, ready):
        conn = db.open_db()
        with db.write_transaction(conn):
            conn.execute("UPDATE topics SET status = 'used'")
        conn.close()

        result = run("topics", "list", "demo")

        assert "закончились" in result.output
        assert "factory topics import demo" in result.output


class TestRun:
    def test_once_advances_posts(self, ready, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")

        result = run("run", "--once")

        assert result.exit_code == 0
        assert "создано постов: 3" in result.output

    def test_once_and_loop_together_are_refused(self, ready):
        result = run("run", "--once", "--loop")

        assert result.exit_code == 1
        assert "factory run --once" in result.output

    def test_neither_flag_is_refused(self, ready):
        result = run("run")

        assert result.exit_code == 1

    def test_second_run_reports_the_lock(self, ready):
        conn = db.open_db()
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES ('tick_lock', ?, ?)",
                (
                    '{"holder": "другой", "pid": 999999, "token": "x", '
                    '"expires_at": "2099-01-01T00:00:00Z"}',
                    to_iso(now_utc()),
                ),
            )
        conn.close()

        result = run("run", "--once")

        assert result.exit_code == 0
        assert "уже работает другой процесс" in result.output


class TestUnlock:
    def test_removes_a_stuck_lock(self, ready):
        conn = db.open_db()
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES ('tick_lock', '{}', ?)",
                (to_iso(now_utc()),),
            )
        conn.close()

        result = run("unlock")

        assert "Блокировка снята" in result.output

    def test_says_when_there_was_nothing_to_unlock(self, ready):
        assert "снимать нечего" in run("unlock").output


class TestPost:
    def test_create_makes_one_post(self, ready):
        result = run("post", "create", "demo")

        assert result.exit_code == 0
        assert "factory post show" in result.output

    def test_create_without_topics_is_explained(self, ready):
        conn = db.open_db()
        with db.write_transaction(conn):
            conn.execute("UPDATE topics SET status = 'used'")
        conn.close()

        result = run("post", "create", "demo")

        assert result.exit_code == 1
        assert "factory topics import demo" in result.output

    def test_show_displays_the_essentials(self, ready, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        run("run", "--once")

        result = run("post", "show", "1")

        assert result.exit_code == 0
        assert "состояние:" in result.output
        assert "Картинок:" in result.output
        assert "Потрачено:" in result.output

    def test_show_explains_deleted_images_of_a_published_post(self, ready, monkeypatch):
        """После публикации файлы удаляются намеренно — это не поломка.

        «Нет файла» рядом с опубликованным постом читается как авария, хотя всё
        в порядке; а вот у неопубликованного пропавший файл — настоящая проблема,
        и её надо называть громко.
        """
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        for _ in range(4):
            run("run", "--once")

        conn = db.open_db()
        published = conn.execute(
            "SELECT id FROM posts WHERE state = 'published' ORDER BY id LIMIT 1"
        ).fetchone()
        conn.close()
        assert published is not None, "за четыре тика ни один пост не опубликовался"

        result = run("post", "show", str(published["id"]))

        assert "удалена после публикации" in result.output
        assert "ФАЙЛ ПРОПАЛ" not in result.output

    def test_show_shouts_when_an_unpublished_post_lost_its_image(self, ready, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        run("run", "--once")
        conn = db.open_db()
        with db.write_transaction(conn):
            conn.execute("UPDATE assets SET local_path = '/нет/такого.png' WHERE post_id = 1")
        conn.close()

        result = run("post", "show", "1")

        assert "ФАЙЛ ПРОПАЛ" in result.output

    def test_show_prints_time_in_the_project_timezone(self, ready, monkeypatch):
        """Владелец живёт по Москве, а не по UTC."""
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        conn = db.open_db()
        post_id = insert_post(conn, 1, 1, state=State.PUBLISHED, idem_key="demo:1:9")
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET external_id = 'vk_1', published_at = ? WHERE id = ?",
                ("2026-08-23T16:35:00Z", post_id),
            )
        conn.close()

        result = run("post", "show", str(post_id))

        assert "23.08.2026 19:35" in result.output, "время показано не в поясе проекта"

    def test_show_unknown_post_is_explained(self, ready):
        result = run("post", "show", "999")

        assert result.exit_code == 1
        assert "factory post list" in result.output
        assert_no_traceback(result)

    def test_show_reports_the_last_error(self, ready):
        conn = db.open_db()
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO posts (project_id, topic_id, idem_key, state, retry_count, "
                "last_error, created_at, updated_at) VALUES (1, 1, 'demo:1:9', 'queued', 2, "
                "?, ?, ?)",
                ("Не найден токен VK.\nЧто делать: см. RUNBOOK.md", to_iso(now_utc()), to_iso(now_utc())),
            )
        conn.close()

        result = run("post", "show", "1")

        assert "Не найден токен VK." in result.output
        assert "попытка 2" in result.output

    def test_list_shows_states(self, ready):
        run("post", "create", "demo")

        result = run("post", "list", "demo")

        assert "queued" in result.output

    def test_list_on_empty_database_says_so(self, ready):
        assert "Постов пока нет" in run("post", "list").output

    def test_retry_clears_the_error(self, ready):
        conn = db.open_db()
        post_id = insert_post(conn, 1, 1, state=State.TEXT_READY, idem_key="demo:1:9")
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET retry_count = 4, last_error = 'таймаут' WHERE id = ?", (post_id,)
            )
        conn.close()

        result = run("post", "retry", str(post_id))

        assert result.exit_code == 0
        conn = db.open_db()
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        conn.close()
        assert row["retry_count"] == 0
        assert row["last_error"] is None

    def test_retry_returns_a_failed_post_to_the_start(self, ready):
        conn = db.open_db()
        post_id = insert_post(conn, 1, 1, state=State.FAILED, idem_key="demo:1:9")
        conn.close()

        result = run("post", "retry", str(post_id))

        assert "в начало цепочки" in result.output
        conn = db.open_db()
        state = conn.execute("SELECT state FROM posts WHERE id = ?", (post_id,)).fetchone()["state"]
        conn.close()
        assert state == State.QUEUED

    def test_retry_of_a_published_post_is_refused(self, ready):
        """Перезапуск опубликованного означал бы второй пост в группе."""
        conn = db.open_db()
        post_id = insert_post(conn, 1, 1, state=State.PUBLISHED, idem_key="demo:1:9")
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET external_id = 'vk_1', published_at = ? WHERE id = ?",
                (to_iso(now_utc()), post_id),
            )
        conn.close()

        result = run("post", "retry", str(post_id))

        assert result.exit_code == 1
        assert "ВКонтакте" in result.output

    def test_reject_frees_the_topic(self, ready):
        run("post", "create", "demo")

        result = run("post", "reject", "1", "--reason", "trash")

        assert result.exit_code == 0
        conn = db.open_db()
        status = conn.execute("SELECT status FROM topics WHERE id = 1").fetchone()["status"]
        conn.close()
        assert status == "free"

    def test_reject_with_a_bad_reason_lists_the_valid_ones(self, ready):
        run("post", "create", "demo")

        result = run("post", "reject", "1", "--reason", "не понравилось")

        assert result.exit_code == 1
        assert "images" in result.output


class TestDoctor:
    def test_healthy_system_returns_zero(self, ready, tmp_path, monkeypatch):
        """Тем должно быть заметно больше буфера, иначе очередь опустеет за один тик."""
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "0")
        many = tmp_path / "many.txt"
        many.write_text("\n".join(f"Тема номер {i}" for i in range(30)), encoding="utf-8")
        run("topics", "import", "demo", str(many))
        run("run", "--once")

        result = run("doctor")

        assert result.exit_code == 0
        assert "Всё в порядке" in result.output

    def test_missing_project_is_reported(self, tmp_env, demo_project):
        run("init")

        result = run("doctor")

        assert result.exit_code == 1
        assert "не подключено ни одного проекта" in result.output

    def test_empty_topic_queue_is_reported(self, ready, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "0")
        conn = db.open_db()
        with db.write_transaction(conn):
            conn.execute("UPDATE topics SET status = 'used'")
        conn.close()

        result = run("doctor")

        assert result.exit_code == 1
        assert "закончились темы" in result.output

    def test_ignore_schedule_is_flagged(self, ready, monkeypatch):
        """Переменная для разработки не должна тихо жить в бою."""
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        run("run", "--once")

        result = run("doctor")

        assert result.exit_code == 1
        assert "FACTORY_IGNORE_SCHEDULE" in result.output

    def test_unwritable_data_dir_gives_advice(self, tmp_env, monkeypatch, tmp_path):
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        monkeypatch.setenv("FACTORY_DATA_DIR", str(readonly / "data"))

        try:
            result = run("doctor")
        finally:
            readonly.chmod(0o700)

        assert result.exit_code == 1
        assert "export FACTORY_DATA_DIR=" in result.output
        assert_no_traceback(result)


class TestStats:
    def test_shows_states_and_money(self, ready, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        run("run", "--once")

        result = run("stats", "demo")

        assert result.exit_code == 0
        assert "Посты по состояниям" in result.output
        assert "Потрачено всего" in result.output


class TestHelp:
    def test_every_command_has_a_russian_help_line(self):
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        for command in ["init", "doctor", "unlock", "run", "project", "topics", "post", "stats"]:
            assert command in result.output

    @pytest.mark.parametrize(
        "args",
        [["init"], ["doctor"], ["run"], ["project", "add"], ["topics", "import"], ["post", "show"]],
    )
    def test_help_text_is_in_russian(self, args):
        result = runner.invoke(app, [*args, "--help"])

        assert result.exit_code == 0
        assert any("Ѐ" <= ch <= "ӿ" for ch in result.output), (
            f"у команды {' '.join(args)} справка не на русском"
        )
