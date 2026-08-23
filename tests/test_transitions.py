"""Шаги пайплайна: по тесту на каждый переход плюс идемпотентность."""

from pathlib import Path

import pytest

from factory.core import db, paths
from factory.core.clock import now_utc, to_iso
from factory.core.config import load_project
from factory.core.errors import FactoryError
from factory.core.logging import get_logger
from factory.core.models import TERMINAL_STATES, TRANSITIONS, Post, State
from factory.core.steps import REGISTRY, Outcome, StepContext, handler_for
from factory.providers.registry import build_providers
from tests.conftest import insert_post, insert_project, insert_topic


@pytest.fixture
def pipeline(conn, demo_project):
    """Проект demo, одна тема, один пост в состоянии queued."""
    project = load_project("demo")
    project_id = insert_project(conn, "demo")
    topic_id = insert_topic(conn, project_id, "Как выбрать шины на зиму")
    post_id = insert_post(conn, project_id, topic_id, idem_key=f"demo:{topic_id}:0")
    conn.commit()

    providers = build_providers(project)

    def context(state: str | None = None) -> StepContext:
        if state is not None:
            with db.write_transaction(conn):
                conn.execute("UPDATE posts SET state = ? WHERE id = ?", (state, post_id))
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return StepContext(
            conn=conn,
            project=project,
            post=Post.from_row(row),
            providers=providers,
            log=get_logger("test"),
        )

    def run(state: str):
        ctx = context(state)
        return handler_for(state)(ctx), ctx

    def advance_through(*states: str):
        for state in states:
            result, _ = run(state)
            assert result.advanced, f"шаг {state} не продвинулся: {result.reason}"

    return {
        "conn": conn,
        "project": project,
        "project_id": project_id,
        "topic_id": topic_id,
        "post_id": post_id,
        "providers": providers,
        "context": context,
        "run": run,
        "advance_through": advance_through,
    }


def post_row(conn, post_id):
    return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


def assets_of(conn, post_id):
    return conn.execute(
        "SELECT * FROM assets WHERE post_id = ? ORDER BY position", (post_id,)
    ).fetchall()


class TestRegistryShape:
    def test_every_non_terminal_state_has_a_handler(self):
        assert set(REGISTRY) == set(TRANSITIONS)

    def test_no_terminal_state_has_a_handler(self):
        assert TERMINAL_STATES.isdisjoint(REGISTRY)

    def test_missing_handler_explains_itself(self):
        with pytest.raises(FactoryError, match="нет обработчика"):
            handler_for(State.PUBLISHED)


class TestText:
    def test_advances_and_fills_the_text(self, pipeline):
        result, _ = pipeline["run"](State.QUEUED)

        assert result.outcome is Outcome.ADVANCED
        assert result.next_state == State.TEXT_READY

        row = post_row(pipeline["conn"], pipeline["post_id"])
        assert row["title"] and row["body"] and row["question"]

    def test_title_fits_the_cover(self, pipeline):
        pipeline["run"](State.QUEUED)

        assert len(post_row(pipeline["conn"], pipeline["post_id"])["title"]) <= 60

    def test_prompt_carries_the_topic_and_the_persona(self, pipeline):
        from factory.core.steps.text import build_prompt

        system, user = build_prompt(pipeline["context"](State.QUEUED))

        assert "Кристина" in system
        assert "Как выбрать шины на зиму" in user
        assert "900-1400" in user

    def test_prompt_contains_no_text_meant_for_humans(self, pipeline):
        """Модель читает всё, что ей дали, как часть задания."""
        from factory.core.steps.text import build_prompt

        system, user = build_prompt(pipeline["context"](State.QUEUED))

        for marker in ["few-shot", "Этап", "config.yaml", "заглушк"]:
            assert marker not in system, f"в системный промпт попало служебное: «{marker}»"

    def test_repeat_run_does_not_call_the_llm_again(self, pipeline):
        """Текст уже оплачен — повторный вызов сжёг бы деньги впустую."""
        pipeline["run"](State.QUEUED)
        before = pipeline["providers"].llm.calls

        result, _ = pipeline["run"](State.QUEUED)

        assert result.advanced
        assert pipeline["providers"].llm.calls == before

    def test_repeat_run_keeps_the_original_text(self, pipeline):
        pipeline["run"](State.QUEUED)
        original = post_row(pipeline["conn"], pipeline["post_id"])["body"]

        pipeline["run"](State.QUEUED)

        assert post_row(pipeline["conn"], pipeline["post_id"])["body"] == original


class TestFactcheck:
    def test_advances_and_records_the_verdict(self, pipeline):
        pipeline["advance_through"](State.QUEUED)
        result, _ = pipeline["run"](State.TEXT_READY)

        assert result.next_state == State.FACTCHECKED
        assert post_row(pipeline["conn"], pipeline["post_id"])["factcheck_verdict"] == "ok"

    def test_disabled_factcheck_skips_the_llm(self, pipeline):
        """content.factcheck: off — шаг проходит, но провайдер не дёргается."""
        project = pipeline["project"]
        pipeline["project"] = project.model_copy(
            update={"content": project.content.model_copy(update={"factcheck": "off"})}
        )
        pipeline["advance_through"](State.QUEUED)
        before = pipeline["providers"].llm.calls

        ctx = pipeline["context"](State.TEXT_READY)
        ctx.project = pipeline["project"]
        result = handler_for(State.TEXT_READY)(ctx)

        assert result.advanced
        assert pipeline["providers"].llm.calls == before
        assert post_row(pipeline["conn"], pipeline["post_id"])["factcheck_verdict"] is None

    def test_repeat_run_does_not_call_the_llm_again(self, pipeline):
        pipeline["advance_through"](State.QUEUED)
        pipeline["run"](State.TEXT_READY)
        before = pipeline["providers"].llm.calls

        pipeline["run"](State.TEXT_READY)

        assert pipeline["providers"].llm.calls == before


class TestPrompts:
    def test_creates_one_cover_and_the_configured_inlines(self, pipeline):
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY)
        result, _ = pipeline["run"](State.FACTCHECKED)

        assert result.next_state == State.PROMPTS_READY

        rows = assets_of(pipeline["conn"], pipeline["post_id"])
        assert len(rows) == 1 + pipeline["project"].image.inline_count
        assert [row["kind"] for row in rows] == ["cover", "inline", "inline", "inline"]

    def test_files_are_not_created_yet(self, pipeline):
        """Промпты и генерация разделены: краш между ними не теряет оплаченного."""
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY)
        pipeline["run"](State.FACTCHECKED)

        assert all(row["local_path"] is None for row in assets_of(pipeline["conn"], pipeline["post_id"]))

    def test_scene_style_from_the_config_is_appended(self, pipeline):
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY)
        pipeline["run"](State.FACTCHECKED)

        for row in assets_of(pipeline["conn"], pipeline["post_id"]):
            assert row["prompt"].endswith(pipeline["project"].image.scene_style)

    def test_every_asset_gets_a_seed(self, pipeline):
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY)
        pipeline["run"](State.FACTCHECKED)

        assert all(row["seed"] for row in assets_of(pipeline["conn"], pipeline["post_id"]))

    def test_repeat_run_does_not_duplicate_assets(self, pipeline):
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY)
        pipeline["run"](State.FACTCHECKED)
        before = len(assets_of(pipeline["conn"], pipeline["post_id"]))

        pipeline["run"](State.FACTCHECKED)

        assert len(assets_of(pipeline["conn"], pipeline["post_id"])) == before


class TestImages:
    def test_generates_every_file(self, pipeline):
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        result, _ = pipeline["run"](State.PROMPTS_READY)

        assert result.next_state == State.IMAGES_READY
        for row in assets_of(pipeline["conn"], pipeline["post_id"]):
            assert row["local_path"]
            assert Path(row["local_path"]).is_file()

    def test_files_land_in_the_per_post_directory(self, pipeline):
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        pipeline["run"](State.PROMPTS_READY)

        expected = paths.post_tmp_dir(pipeline["post_id"])
        for row in assets_of(pipeline["conn"], pipeline["post_id"]):
            assert Path(row["local_path"]).parent == expected

    def test_already_generated_images_are_not_paid_for_twice(self, pipeline):
        """Главный тест идемпотентности: картинки стоят денег."""
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        pipeline["run"](State.PROMPTS_READY)
        calls_after_first = pipeline["providers"].images.calls

        pipeline["run"](State.PROMPTS_READY)

        assert pipeline["providers"].images.calls == calls_after_first

    def test_only_the_missing_half_is_generated(self, pipeline):
        """Краш посреди генерации: доделывается остаток, а не всё заново."""
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        pipeline["run"](State.PROMPTS_READY)

        rows = assets_of(pipeline["conn"], pipeline["post_id"])
        survivors, wiped = rows[:2], rows[2:]
        with db.write_transaction(pipeline["conn"]):
            for row in wiped:
                Path(row["local_path"]).unlink()
                pipeline["conn"].execute(
                    "UPDATE assets SET local_path = NULL WHERE id = ?", (row["id"],)
                )
        before = pipeline["providers"].images.calls

        pipeline["run"](State.PROMPTS_READY)

        assert pipeline["providers"].images.calls == before + len(wiped)
        for row in survivors:
            assert Path(row["local_path"]).is_file()

    def test_vanished_file_is_regenerated(self, pipeline):
        """Путь в базе есть, файла нет — например, почистили /tmp при перезагрузке."""
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        pipeline["run"](State.PROMPTS_READY)
        victim = assets_of(pipeline["conn"], pipeline["post_id"])[0]
        Path(victim["local_path"]).unlink()
        before = pipeline["providers"].images.calls

        result, _ = pipeline["run"](State.PROMPTS_READY)

        assert result.advanced
        assert pipeline["providers"].images.calls == before + 1
        assert Path(victim["local_path"]).is_file()

    def test_seed_from_the_asset_reaches_the_provider(self, pipeline):
        """На seed держится кнопка «Картинки заново»: тот же промпт, новый seed.

        Если seed не доезжает до провайдера, кнопка на Этапе 5 будет возвращать
        те же самые картинки, и понять почему станет очень трудно.
        """
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        seen: list[int | None] = []
        provider = pipeline["providers"].images
        original = provider.generate

        def capture(prompt, *, lora=None, seed=None, **kwargs):
            seen.append(seed)
            return original(prompt, lora=lora, seed=seed, **kwargs)

        provider.generate = capture

        pipeline["run"](State.PROMPTS_READY)

        stored = [row["seed"] for row in assets_of(pipeline["conn"], pipeline["post_id"])]
        assert seen, "провайдер картинок не вызывался"
        assert sorted(seen) == sorted(stored)

    def test_new_seed_produces_a_different_image(self, pipeline):
        """Поведенческая половина того же требования, на настоящих байтах."""
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        pipeline["run"](State.PROMPTS_READY)

        cover = assets_of(pipeline["conn"], pipeline["post_id"])[0]
        before = Path(cover["local_path"]).read_bytes()

        with db.write_transaction(pipeline["conn"]):
            pipeline["conn"].execute(
                "UPDATE assets SET seed = seed + 1, local_path = NULL WHERE id = ?",
                (cover["id"],),
            )
        Path(cover["local_path"]).unlink()

        pipeline["run"](State.PROMPTS_READY)

        assert Path(cover["local_path"]).read_bytes() != before

    def test_parallel_pool_produces_the_same_files(self, pipeline, monkeypatch):
        """FACTORY_MAX_PARALLEL_IMAGES=4 на сервере, 1 на Pi — результат одинаковый."""
        monkeypatch.setenv("FACTORY_MAX_PARALLEL_IMAGES", "4")
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)

        result, _ = pipeline["run"](State.PROMPTS_READY)

        assert result.advanced
        assert all(Path(row["local_path"]).is_file() for row in assets_of(pipeline["conn"], pipeline["post_id"]))


class TestCompose:
    def test_advances_when_everything_is_in_place(self, pipeline):
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )
        result, _ = pipeline["run"](State.IMAGES_READY)

        assert result.next_state == State.COMPOSED

    def test_missing_cover_file_is_reported_understandably(self, pipeline):
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )
        cover = assets_of(pipeline["conn"], pipeline["post_id"])[0]
        Path(cover["local_path"]).unlink()

        with pytest.raises(FactoryError) as excinfo:
            pipeline["run"](State.IMAGES_READY)

        assert "factory post retry" in str(excinfo.value)

    def test_missing_title_is_reported_understandably(self, pipeline):
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )
        with db.write_transaction(pipeline["conn"]):
            pipeline["conn"].execute(
                "UPDATE posts SET title = NULL WHERE id = ?", (pipeline["post_id"],)
            )

        with pytest.raises(FactoryError, match="заголовка"):
            pipeline["run"](State.IMAGES_READY)


class TestReview:
    def test_auto_mode_walks_through_to_approved(self, pipeline):
        pipeline["advance_through"](
            State.QUEUED,
            State.TEXT_READY,
            State.FACTCHECKED,
            State.PROMPTS_READY,
            State.IMAGES_READY,
        )
        first, _ = pipeline["run"](State.COMPOSED)
        second, _ = pipeline["run"](State.IN_REVIEW)

        assert first.next_state == State.IN_REVIEW
        assert second.next_state == State.APPROVED

    def test_telegram_mode_waits_instead_of_failing(self, pipeline):
        """WAITING не наращивает retry_count — пост может ждать человека неделю."""
        project = pipeline["project"]
        telegram = project.model_copy(
            update={"review": project.review.model_copy(update={"mode": "telegram"})}
        )
        ctx = pipeline["context"](State.IN_REVIEW)
        ctx.project = telegram

        result = handler_for(State.IN_REVIEW)(ctx)

        assert result.outcome is Outcome.WAITING
        assert "Telegram" in result.reason


class TestPublish:
    def test_publishes_and_records_the_result(self, pipeline, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        pipeline["advance_through"](
            State.QUEUED,
            State.TEXT_READY,
            State.FACTCHECKED,
            State.PROMPTS_READY,
            State.IMAGES_READY,
            State.COMPOSED,
            State.IN_REVIEW,
        )
        result, _ = pipeline["run"](State.APPROVED)

        assert result.next_state == State.PUBLISHED
        row = post_row(pipeline["conn"], pipeline["post_id"])
        assert row["external_id"] == f"stub_{pipeline['post_id']}"
        assert row["published_at"] is not None

    def test_topic_is_marked_used(self, pipeline, monkeypatch):
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        pipeline["advance_through"](
            State.QUEUED,
            State.TEXT_READY,
            State.FACTCHECKED,
            State.PROMPTS_READY,
            State.IMAGES_READY,
            State.COMPOSED,
            State.IN_REVIEW,
        )
        pipeline["run"](State.APPROVED)

        status = pipeline["conn"].execute(
            "SELECT status FROM topics WHERE id = ?", (pipeline["topic_id"],)
        ).fetchone()["status"]
        assert status == "used"

    def test_already_published_post_is_not_published_again(self, pipeline, monkeypatch):
        """Дубль поста в группе недопустим — проверка стоит на external_id."""
        monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", "1")
        with db.write_transaction(pipeline["conn"]):
            pipeline["conn"].execute(
                "UPDATE posts SET external_id = 'vk_777', published_at = ? WHERE id = ?",
                (to_iso(now_utc()), pipeline["post_id"]),
            )
        before = pipeline["providers"].publisher.calls

        result, _ = pipeline["run"](State.APPROVED)

        assert result.advanced
        assert pipeline["providers"].publisher.calls == before
        assert post_row(pipeline["conn"], pipeline["post_id"])["external_id"] == "vk_777"
