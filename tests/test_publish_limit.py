"""Расписание публикаций и дневной лимит.

Оба ограничения возвращают WAITING, а не ошибку: пост исправен, просто сейчас не
его очередь. Ошибка наращивала бы retry_count и через пять тиков убила бы
нормальный пост.
"""

from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import pytest

from factory.core import db, paths
from factory.core.clock import now_utc, to_iso
from factory.core.config import load_project
from factory.core.logging import get_logger
from factory.core.models import Post, State
from factory.core.steps import Outcome, StepContext
from factory.core.steps.publish import open_slot, published_today, run
from factory.providers.registry import build_providers
from tests.conftest import insert_post, insert_project, insert_topic

MOSCOW = ZoneInfo("Europe/Moscow")


@pytest.fixture
def ready_post(conn, demo_project):
    """Проект demo и один пост, готовый к публикации."""
    project = load_project("demo")
    project_id = insert_project(conn, "demo")
    topic_id = insert_topic(conn, project_id, "Тема")
    post_id = insert_post(conn, project_id, topic_id, state=State.APPROVED)
    conn.commit()

    env: dict = {
        "conn": conn,
        "project": project,
        "project_id": project_id,
        "post_id": post_id,
    }

    def context() -> StepContext:
        # Конфиг берётся из env на момент вызова, а не захватывается замыканием:
        # тесты подменяют env["project"], и захваченная копия молча игнорировала
        # бы подмену — тест проверял бы совсем не то, что заявляет.
        current = env["project"]
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return StepContext(
            conn=conn,
            project=current,
            post=Post.from_row(row),
            providers=build_providers(current),
            log=get_logger("test"),
        )

    env["context"] = context
    return env


def publish_a_post(conn, project_id, *, at, suffix):
    """Кладёт в базу уже опубликованный пост с заданным published_at."""
    topic_id = insert_topic(conn, project_id, f"Тема {suffix}")
    post_id = insert_post(
        conn, project_id, topic_id, state=State.PUBLISHED, idem_key=f"demo:{topic_id}:0"
    )
    with db.write_transaction(conn):
        conn.execute(
            "UPDATE posts SET external_id = ?, published_at = ?, updated_at = ? WHERE id = ?",
            (f"vk_{suffix}", to_iso(at), to_iso(now_utc()), post_id),
        )
    return post_id


class TestDailyLimit:
    def test_reaching_the_limit_gives_waiting_not_an_error(self, ready_post, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        for i in range(2):
            publish_a_post(ready_post["conn"], ready_post["project_id"], at=now_utc(), suffix=i)

        result = run(ready_post["context"]())

        assert result.outcome is Outcome.WAITING
        assert "лимит" in result.reason

    def test_under_the_limit_publishes(self, ready_post, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        publish_a_post(ready_post["conn"], ready_post["project_id"], at=now_utc(), suffix=0)

        assert run(ready_post["context"]()).advanced

    def test_yesterdays_posts_do_not_count(self, ready_post, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        yesterday = now_utc() - timedelta(days=1)
        for i in range(5):
            publish_a_post(ready_post["conn"], ready_post["project_id"], at=yesterday, suffix=i)

        assert run(ready_post["context"]()).advanced

    def test_counting_uses_published_at_not_updated_at(self, ready_post, monkeypatch):
        """Вчерашний пост, чью строку тронули сегодня, не должен съедать слот.

        Это и есть причина, по которой published_at существует отдельно от
        updated_at: updated_at меняется при любой правке — post retry, воркер
        комментариев на Этапе 6.
        """
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        conn = ready_post["conn"]
        yesterday = now_utc().astimezone(MOSCOW).replace(hour=23, minute=50) - timedelta(days=1)
        for i in range(2):
            post_id = publish_a_post(conn, ready_post["project_id"], at=yesterday, suffix=i)
            with db.write_transaction(conn):
                conn.execute(
                    "UPDATE posts SET updated_at = ? WHERE id = ?", (to_iso(now_utc()), post_id)
                )

        assert run(ready_post["context"]()).advanced, "вчерашние посты заняли сегодняшний лимит"

    def test_counter_uses_the_project_timezone(self, ready_post, monkeypatch):
        """Момент, у которого московская дата и дата в UTC разные.

        «Сейчас» — 23 августа 22:00 UTC, то есть уже 24 августа 01:00 по Москве.
        Пост вышел 24 августа в 00:30 по Москве, это 23 августа 21:30 UTC.

        По московским суткам оба — «сегодня», по UTC — «вчера». Обе стороны
        сравнения должны считаться в поясе проекта; если хоть одна съедет на UTC,
        счётчик даст 0 и лимит перестанет работать в вечерние часы.
        """
        conn = ready_post["conn"]
        project = ready_post["project"]
        monkeypatch.setattr(
            "factory.core.steps.publish.now_utc",
            lambda: datetime(2026, 8, 23, 22, 0, tzinfo=ZoneInfo("UTC")),
        )
        just_after_midnight_msk = datetime(2026, 8, 24, 0, 30, tzinfo=MOSCOW)
        publish_a_post(conn, ready_post["project_id"], at=just_after_midnight_msk, suffix=0)

        counted = published_today(conn, project, ready_post["project_id"])

        assert counted == 1, "пост из московского «сегодня» посчитан по UTC и потерян"

    def test_concurrent_publish_does_not_overwrite_the_recorded_id(self, ready_post, monkeypatch):
        """Между проверкой external_id и записью другой тик успел опубликовать.

        Проверка повторяется прямо в UPDATE (`AND external_id IS NULL`), поэтому
        наша запись не затирает чужую: в группе остаётся один пост, а не два с
        разными идентификаторами.
        """
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        conn = ready_post["conn"]
        post_id = ready_post["post_id"]
        ctx = ready_post["context"]()
        original = ctx.providers.publisher.publish

        def publish_then_lose_the_race(post, assets):
            external_id = original(post, assets)
            with db.write_transaction(conn):
                conn.execute(
                    "UPDATE posts SET external_id = 'vk_from_other_tick', published_at = ? "
                    "WHERE id = ?",
                    (to_iso(now_utc()), post_id),
                )
            return external_id

        ctx.providers.publisher.publish = publish_then_lose_the_race

        result = run(ctx)

        assert result.advanced
        row = conn.execute("SELECT external_id FROM posts WHERE id = ?", (post_id,)).fetchone()
        assert row["external_id"] == "vk_from_other_tick"

    def test_other_projects_do_not_share_the_limit(self, ready_post, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        conn = ready_post["conn"]
        other_id = insert_project(conn, "other")
        for i in range(5):
            publish_a_post(conn, other_id, at=now_utc(), suffix=f"other{i}")

        assert run(ready_post["context"]()).advanced


class TestSchedule:
    def test_inside_a_slot_the_post_actually_goes_out(self, ready_post, monkeypatch):
        """Полный путь через run(), а не только open_slot: без FACTORY_IGNORE_SCHEDULE."""
        monkeypatch.delenv("FACTORY_IGNORE_SCHEDULE", raising=False)
        monkeypatch.setattr(
            "factory.core.steps.publish.now_utc",
            lambda: datetime(2026, 8, 23, 19, 35, tzinfo=MOSCOW),
        )

        result = run(ready_post["context"]())

        assert result.advanced
        assert ready_post["conn"].execute(
            "SELECT external_id FROM posts WHERE id = ?", (ready_post["post_id"],)
        ).fetchone()["external_id"]

    def test_slot_is_recognised_by_open_slot(self, ready_post):
        at = datetime.now(MOSCOW).replace(hour=19, minute=35, second=0, microsecond=0)

        assert open_slot(ready_post["project"], at) == time(19, 30)

    def test_just_before_a_slot_is_closed(self, ready_post):
        at = datetime.now(MOSCOW).replace(hour=19, minute=29, second=0, microsecond=0)

        assert open_slot(ready_post["project"], at) is None

    def test_slot_stays_open_for_an_hour(self, ready_post):
        project = ready_post["project"]
        inside = datetime.now(MOSCOW).replace(hour=20, minute=29, second=0, microsecond=0)
        outside = datetime.now(MOSCOW).replace(hour=20, minute=31, second=0, microsecond=0)

        assert open_slot(project, inside) == time(19, 30)
        assert open_slot(project, outside) is None

    def test_second_slot_is_recognised(self, ready_post):
        at = datetime.now(MOSCOW).replace(hour=21, minute=5, second=0, microsecond=0)

        assert open_slot(ready_post["project"], at) == time(21, 0)

    def test_slots_are_matched_in_the_project_timezone(self, ready_post):
        """Слот определяется по московскому времени, в каком бы поясе ни пришёл момент.

        16:35 UTC и 19:35 по Москве — один и тот же момент, и оба обязаны попасть
        в слот 19:30. Если сравнение съедет на UTC, слот не найдётся вовсе.
        """
        project = ready_post["project"]
        moment = datetime(2026, 8, 23, 19, 35, tzinfo=MOSCOW)

        assert open_slot(project, moment) == time(19, 30)
        assert open_slot(project, moment.astimezone(ZoneInfo("UTC"))) == time(19, 30)


class TestSlotAcrossMidnight:
    """Окно слота не должно обрываться о полночь.

    У слота 23:30 час окна заканчивается в 00:30 следующих суток. Привязка окна
    только к сегодняшней дате обрезала бы его до 00:00, а для слота 23:55
    реальное окно составило бы пять минут — один пропущенный тик молча съедал бы
    дневную публикацию, без ошибки и без роста retry_count.
    """

    @pytest.fixture
    def late_night(self, ready_post):
        project = ready_post["project"]
        late = project.model_copy(
            update={"vk": project.vk.model_copy(update={"schedule": ["23:30"]})}
        )
        ready_post["project"] = late
        return ready_post

    @pytest.mark.parametrize(
        "moment,expected",
        [
            (datetime(2026, 8, 23, 23, 31, tzinfo=MOSCOW), time(23, 30)),
            (datetime(2026, 8, 23, 23, 59, tzinfo=MOSCOW), time(23, 30)),
            (datetime(2026, 8, 24, 0, 10, tzinfo=MOSCOW), time(23, 30)),
            (datetime(2026, 8, 24, 0, 29, tzinfo=MOSCOW), time(23, 30)),
            (datetime(2026, 8, 24, 0, 31, tzinfo=MOSCOW), None),
            (datetime(2026, 8, 23, 23, 29, tzinfo=MOSCOW), None),
        ],
    )
    def test_window_continues_past_midnight(self, late_night, moment, expected):
        assert open_slot(late_night["project"], moment) == expected

    def test_a_post_actually_publishes_after_midnight(self, late_night, monkeypatch):
        monkeypatch.delenv("FACTORY_IGNORE_SCHEDULE", raising=False)
        monkeypatch.setattr(
            "factory.core.steps.publish.now_utc",
            lambda: datetime(2026, 8, 24, 0, 10, tzinfo=MOSCOW),
        )

        assert run(late_night["context"]()).advanced

    def test_slot_is_still_used_only_once_across_midnight(self, late_night, monkeypatch):
        monkeypatch.delenv("FACTORY_IGNORE_SCHEDULE", raising=False)
        monkeypatch.setattr(
            "factory.core.steps.publish.now_utc",
            lambda: datetime(2026, 8, 24, 0, 10, tzinfo=MOSCOW),
        )
        publish_a_post(
            late_night["conn"],
            late_night["project_id"],
            at=datetime(2026, 8, 23, 23, 40, tzinfo=MOSCOW),
            suffix=0,
        )

        result = run(late_night["context"]())

        assert result.outcome is Outcome.WAITING
        assert "слоте" in result.reason


class TestSlotIsUsedOnce:
    """Один слот — одна публикация.

    Иначе весь дневной лимит выливается в первое открытое окно: два поста с
    разницей в десять минут вечером и пусто во втором слоте, который владелец
    как раз и выбирал.
    """

    def test_second_post_in_the_same_slot_waits(self, ready_post, monkeypatch):
        monkeypatch.delenv("FACTORY_IGNORE_SCHEDULE", raising=False)
        monkeypatch.setattr(
            "factory.core.steps.publish.now_utc",
            lambda: datetime(2026, 8, 23, 19, 40, tzinfo=MOSCOW),
        )
        publish_a_post(
            ready_post["conn"],
            ready_post["project_id"],
            at=datetime(2026, 8, 23, 19, 32, tzinfo=MOSCOW),
            suffix=0,
        )

        result = run(ready_post["context"]())

        assert result.outcome is Outcome.WAITING
        assert "слоте" in result.reason

    def test_the_next_slot_is_still_open(self, ready_post, monkeypatch):
        monkeypatch.delenv("FACTORY_IGNORE_SCHEDULE", raising=False)
        monkeypatch.setattr(
            "factory.core.steps.publish.now_utc",
            lambda: datetime(2026, 8, 23, 21, 5, tzinfo=MOSCOW),
        )
        publish_a_post(
            ready_post["conn"],
            ready_post["project_id"],
            at=datetime(2026, 8, 23, 19, 32, tzinfo=MOSCOW),
            suffix=0,
        )

        assert run(ready_post["context"]()).advanced

    def test_yesterdays_publication_does_not_close_todays_slot(self, ready_post, monkeypatch):
        monkeypatch.delenv("FACTORY_IGNORE_SCHEDULE", raising=False)
        monkeypatch.setattr(
            "factory.core.steps.publish.now_utc",
            lambda: datetime(2026, 8, 23, 19, 40, tzinfo=MOSCOW),
        )
        publish_a_post(
            ready_post["conn"],
            ready_post["project_id"],
            at=datetime(2026, 8, 22, 19, 32, tzinfo=MOSCOW),
            suffix=0,
        )

        assert run(ready_post["context"]()).advanced


class TestAfterPublishing:
    def test_temporary_files_are_removed(self, ready_post, monkeypatch):
        """Иначе картинки копятся вечно: на Pi это гигабайты в год, местами в оперативке."""
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        target = paths.post_tmp_dir(ready_post["post_id"])
        target.mkdir(parents=True, exist_ok=True)
        (target / "cover_0.png").write_bytes(b"\x89PNG")

        run(ready_post["context"]())

        assert not target.exists()

    def test_failure_to_delete_does_not_undo_the_publication(self, ready_post, monkeypatch):
        """Пост уже в группе. Отказ из-за неудалённого файла был бы хуже мусора."""
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")

        def refuse(path, ignore_errors=False):
            raise OSError("устройство занято")

        monkeypatch.setattr("factory.core.steps.publish.shutil.rmtree", refuse)

        assert run(ready_post["context"]()).advanced
        assert ready_post["conn"].execute(
            "SELECT external_id FROM posts WHERE id = ?", (ready_post["post_id"],)
        ).fetchone()["external_id"]

    def test_cover_is_the_first_attachment(self, ready_post, monkeypatch):
        """Первое вложение ВК показывает главным. Обложка с заголовком обязана быть им."""
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        conn = ready_post["conn"]
        with db.write_transaction(conn):
            for kind, position in [("cover", 0), ("inline", 1), ("inline", 2), ("inline", 3)]:
                conn.execute(
                    "INSERT INTO assets (post_id, kind, position, local_path, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ready_post["post_id"], kind, position, f"/tmp/{kind}_{position}.png",
                     to_iso(now_utc())),
                )
        seen: list[list] = []
        ctx = ready_post["context"]()
        original = ctx.providers.publisher.publish

        def capture(post, assets):
            seen.append(list(assets))
            return original(post, assets)

        ctx.providers.publisher.publish = capture

        run(ctx)

        (attachments,) = seen
        assert [a.kind for a in attachments] == ["cover", "inline", "inline", "inline"]


class TestNoRetryOnPublish:
    def test_a_timed_out_publish_is_not_repeated(self, ready_post, monkeypatch):
        """Таймаут не значит «не опубликовалось»: ВК мог создать пост и не ответить.

        Повтор положил бы в стену второй такой же. Поэтому у шага публикации
        ретраев нет вовсе — в отличие от всех остальных шагов.
        """
        import httpx

        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        calls = []
        ctx = ready_post["context"]()

        def publish_and_time_out(post, assets):
            calls.append(1)
            raise httpx.ReadTimeout("ответ не доехал")

        ctx.providers.publisher.publish = publish_and_time_out

        with pytest.raises(httpx.ReadTimeout):
            run(ctx)

        assert len(calls) == 1, f"публикация повторена {len(calls)} раз вместо одного"

    def test_outside_the_schedule_gives_waiting(self, ready_post, monkeypatch):
        monkeypatch.delenv("FACTORY_IGNORE_SCHEDULE", raising=False)
        monkeypatch.setattr(
            "factory.core.steps.publish.now_utc",
            lambda: datetime(2026, 8, 23, 4, 0, tzinfo=MOSCOW),
        )

        result = run(ready_post["context"]())

        assert result.outcome is Outcome.WAITING
        assert "расписания" in result.reason

    def test_ignore_schedule_env_bypasses_the_check(self, ready_post, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        monkeypatch.setattr(
            "factory.core.steps.publish.now_utc",
            lambda: datetime(2026, 8, 23, 4, 0, tzinfo=MOSCOW),
        )

        assert run(ready_post["context"]()).advanced

    def test_daily_limit_is_reported_even_outside_the_schedule(self, ready_post, monkeypatch):
        """Момент нарочно ВНЕ слотов: только так видно, какая проверка сработала первой.

        Внутри слота обе проверки дали бы WAITING, и порядок остался бы неизвестен.
        Даты фиксированные — тест с настоящими часами сломался бы через два дня.
        """
        monkeypatch.delenv("FACTORY_IGNORE_SCHEDULE", raising=False)
        monkeypatch.setattr(
            "factory.core.steps.publish.now_utc",
            lambda: datetime(2026, 8, 23, 4, 0, tzinfo=MOSCOW),
        )
        for i in range(2):
            publish_a_post(
                ready_post["conn"],
                ready_post["project_id"],
                at=datetime(2026, 8, 23, 1, 0, tzinfo=MOSCOW),
                suffix=i,
            )

        reason = run(ready_post["context"]()).reason

        assert "лимит" in reason
        assert "расписания" not in reason, "проверка расписания сработала раньше лимита"
