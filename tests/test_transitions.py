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


class _Bytes(bytes):
    """Байты, к которым можно прикрепить цену: на голых bytes атрибут не живёт."""


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

    def test_prompt_carries_the_style_examples(self, pipeline):
        """Без примеров посты перестают попадать в стиль, и это молча.

        Ошибки не будет: модель напишет складный текст не тем голосом, а
        владелец увидит это уже в ревью и не поймёт, что сломалось.
        """
        from factory.core.steps.text import build_prompt

        project = pipeline["context"](State.QUEUED).project
        examples = project.style_examples()
        assert examples, "у демо-проекта нет примеров — тест ничего не проверит"

        system, _ = build_prompt(pipeline["context"](State.QUEUED))

        for example in examples:
            assert example.strip() in system

    def test_prompt_carries_the_post_structure(self, pipeline):
        from factory.core.steps.text import build_prompt

        project = pipeline["context"](State.QUEUED).project
        _, user = build_prompt(pipeline["context"](State.QUEUED))

        for block in project.content.post_structure:
            assert block in user

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
        """Провайдер подменяется на другой текст: заглушка детерминирована, и без
        подмены тест проходил бы даже при полностью снятой защите."""
        pipeline["run"](State.QUEUED)
        original = post_row(pipeline["conn"], pipeline["post_id"])["body"]

        from factory.providers.base import PostDraft

        pipeline["providers"].llm.complete = lambda system, user, *, schema=None: PostDraft(
            title="Совсем другой заголовок", body="Совсем другой текст", question="Другой вопрос?"
        )

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
        # Считать надо модель ФАКТЧЕКА: обращения к модели текста тут не меняются
        # никогда, и ассерт по ним истинен при любой реализации шага.
        before = pipeline["providers"].factcheck.calls

        ctx = pipeline["context"](State.TEXT_READY)
        ctx.project = pipeline["project"]
        result = handler_for(State.TEXT_READY)(ctx)

        assert result.advanced
        assert pipeline["providers"].factcheck.calls == before
        assert post_row(pipeline["conn"], pipeline["post_id"])["factcheck_verdict"] is None

    def test_repeat_run_does_not_call_the_llm_again(self, pipeline):
        """Считать надо обращения к модели ФАКТЧЕКА, а не к модели текста.

        Раньше тест смотрел на providers.llm — после разделения моделей он
        перестал что-либо проверять, потому что фактчек туда не ходит вовсе.
        """
        pipeline["advance_through"](State.QUEUED)
        pipeline["run"](State.TEXT_READY)
        before = pipeline["providers"].factcheck.calls
        assert before > 0, "первый фактчек вообще не обратился к модели"

        pipeline["run"](State.TEXT_READY)

        assert pipeline["providers"].factcheck.calls == before

    def test_verdict_fixed_replaces_the_body(self, pipeline):
        """SPEC: «fixed → заменяем body». Заглушка всегда отвечает ok, поэтому
        правило не проверялось ничем — а на Этапе 3 провайдер начнёт возвращать
        fixed по-настоящему."""
        from factory.providers.base import FactcheckResult

        pipeline["advance_through"](State.QUEUED)
        original = post_row(pipeline["conn"], pipeline["post_id"])["body"]
        pipeline["providers"].factcheck.complete = lambda s, u, *, schema=None: FactcheckResult(
            verdict="fixed",
            corrected_body="Исправленный текст с верными датами.",
            notes="поправлен год",
        )

        result, _ = pipeline["run"](State.TEXT_READY)

        row = post_row(pipeline["conn"], pipeline["post_id"])
        assert result.advanced
        assert row["body"] == "Исправленный текст с верными датами."
        assert row["body"] != original
        assert row["factcheck_verdict"] == "fixed"

    def test_verdict_fixed_without_a_body_keeps_the_original(self, pipeline):
        """Провайдер сказал «исправил», но текста не дал — терять исходный нельзя."""
        from factory.providers.base import FactcheckResult

        pipeline["advance_through"](State.QUEUED)
        original = post_row(pipeline["conn"], pipeline["post_id"])["body"]
        pipeline["providers"].factcheck.complete = lambda s, u, *, schema=None: FactcheckResult(
            verdict="fixed", corrected_body=None
        )

        pipeline["run"](State.TEXT_READY)

        assert post_row(pipeline["conn"], pipeline["post_id"])["body"] == original

    def test_uncertain_verdict_is_stored_with_its_notes(self, pipeline):
        """SPEC требует показать «⚠️ фактчек не уверен: {notes}» — значит, notes надо хранить."""
        from factory.providers.base import FactcheckResult

        pipeline["advance_through"](State.QUEUED)
        pipeline["providers"].factcheck.complete = lambda s, u, *, schema=None: FactcheckResult(
            verdict="uncertain", notes="не нашёл подтверждения по дате основания"
        )

        result, _ = pipeline["run"](State.TEXT_READY)

        row = post_row(pipeline["conn"], pipeline["post_id"])
        assert result.advanced, "неуверенный фактчек не должен останавливать пост"
        assert row["factcheck_verdict"] == "uncertain"
        assert "не нашёл подтверждения по дате основания" in row["factcheck_notes"]


class TestFactcheckHonesty:
    """Фактчек не должен выглядеть надёжнее, чем он есть.

    Проверено живьём: модель без веб-поиска одобрила текст, где штраф был
    завышен в сто раз, и сослалась на несуществующий пункт приказа. Вердикт
    «ok» от такой модели ничего не значит — но выглядит как проверка.
    """

    def test_check_without_search_says_so_in_the_notes(self, pipeline):
        from factory.providers.base import FactcheckResult

        pipeline["advance_through"](State.QUEUED)
        assert pipeline["project"].llm.factcheck_web_search is False
        pipeline["providers"].factcheck.complete = lambda s, u, *, schema=None: FactcheckResult(
            verdict="ok", notes="всё сходится"
        )

        pipeline["run"](State.TEXT_READY)

        notes = post_row(pipeline["conn"], pipeline["post_id"])["factcheck_notes"]
        assert "без поиска по источникам" in notes
        assert "всё сходится" in notes

    def test_check_with_search_adds_no_disclaimer(self, pipeline):
        from factory.providers.base import FactcheckResult

        project = pipeline["project"]
        with_search = project.model_copy(
            update={"llm": project.llm.model_copy(update={"factcheck_web_search": True})}
        )
        pipeline["advance_through"](State.QUEUED)

        ctx = pipeline["context"](State.TEXT_READY)
        ctx.project = with_search
        ctx.providers.factcheck.complete = lambda s, u, *, schema=None: FactcheckResult(
            verdict="ok", notes="проверено по источникам"
        )
        handler_for(State.TEXT_READY)(ctx)

        notes = post_row(pipeline["conn"], pipeline["post_id"])["factcheck_notes"]
        assert "без поиска" not in notes

    def test_the_prompt_differs_between_the_two_modes(self, pipeline):
        """Модели без поиска прямо запрещается ставить ok при наличии фактов."""
        from factory.core.steps import factcheck as step

        assert "интернете" in step.SYSTEM_STRICT
        assert "без доступа к интернету" in step.SYSTEM_LIGHT
        assert "uncertain" in step.SYSTEM_LIGHT

    def test_the_step_actually_sends_the_matching_prompt(self, pipeline):
        """Сверять сами константы мало: шаг должен выбирать из них правильную.

        Без этой проверки подмена «всегда строгий промпт» проходит незаметно, и
        модель без поиска получает задание проверять по источникам, которых у
        неё нет.
        """
        from factory.core.steps import factcheck as step
        from factory.providers.base import FactcheckResult

        pipeline["advance_through"](State.QUEUED)
        sent: list[str] = []
        pipeline["providers"].factcheck.complete = lambda s, u, *, schema=None: (
            sent.append(s) or FactcheckResult(verdict="ok")
        )

        assert pipeline["project"].llm.factcheck_web_search is False
        pipeline["run"](State.TEXT_READY)

        assert sent[0] == step.SYSTEM_LIGHT, "модели без поиска ушёл строгий промпт"

    def test_light_mode_wins_over_a_search_capable_model(self, pipeline):
        """«Понизил до light ради экономии» обязано что-то менять.

        Если шаг смотрит только на возможности модели, понижение режима не
        делает ничего: промпт остаётся строгим, поиск оплачивается полностью,
        и владелец об этом не узнает.
        """
        from factory.core.steps import factcheck as step
        from factory.providers.base import FactcheckResult

        project = pipeline["project"]
        cheap = project.model_copy(
            update={
                "content": project.content.model_copy(update={"factcheck": "light"}),
                "llm": project.llm.model_copy(update={"factcheck_web_search": True}),
            }
        )
        pipeline["advance_through"](State.QUEUED)
        sent: list[str] = []

        ctx = pipeline["context"](State.TEXT_READY)
        ctx.project = cheap
        ctx.providers.factcheck.complete = lambda s, u, *, schema=None: (
            sent.append(s) or FactcheckResult(verdict="ok")
        )
        handler_for(State.TEXT_READY)(ctx)

        assert sent[0] == step.SYSTEM_LIGHT
        notes = post_row(pipeline["conn"], pipeline["post_id"])["factcheck_notes"]
        assert "без поиска по источникам" in notes

    def test_with_search_the_strict_prompt_is_sent(self, pipeline):
        from factory.core.steps import factcheck as step
        from factory.providers.base import FactcheckResult

        project = pipeline["project"]
        with_search = project.model_copy(
            update={"llm": project.llm.model_copy(update={"factcheck_web_search": True})}
        )
        pipeline["advance_through"](State.QUEUED)
        sent: list[str] = []

        ctx = pipeline["context"](State.TEXT_READY)
        ctx.project = with_search
        ctx.providers.factcheck.complete = lambda s, u, *, schema=None: (
            sent.append(s) or FactcheckResult(verdict="ok")
        )
        handler_for(State.TEXT_READY)(ctx)

        assert sent[0] == step.SYSTEM_STRICT

    def test_factcheck_uses_its_own_model_not_the_writer(self, pipeline):
        """Иначе экономия на модели текста тихо распространяется на проверку."""
        from factory.providers.base import FactcheckResult

        pipeline["advance_through"](State.QUEUED)
        writer_calls = pipeline["providers"].llm.calls
        used = []
        pipeline["providers"].factcheck.complete = lambda s, u, *, schema=None: (
            used.append(1) or FactcheckResult(verdict="ok")
        )

        pipeline["run"](State.TEXT_READY)

        assert used == [1], "шаг не обратился к модели фактчека"
        assert pipeline["providers"].llm.calls == writer_calls, "дёрнул модель текста"


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

    def test_every_asset_gets_its_own_seed(self, pipeline):
        """Одинаковый seed у всех означал бы четыре одинаковые картинки."""
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY)
        pipeline["run"](State.FACTCHECKED)

        seeds = [row["seed"] for row in assets_of(pipeline["conn"], pipeline["post_id"])]

        assert all(seeds)
        assert len(set(seeds)) == len(seeds), f"seed повторяются: {seeds}"

    def test_inline_count_from_the_config_is_respected(self, pipeline):
        """Число картинок задаёт конфиг, а не то, сколько сцен вернул провайдер.

        Сверять длину с тем же inline_count бессмысленно: заглушка всегда даёт
        ровно столько, сколько нужно, и обрезка не проверяется. Здесь провайдер
        нарочно возвращает больше сцен, чем просили.
        """
        from factory.providers.base import ScenePrompts

        pipeline["advance_through"](State.QUEUED, State.TEXT_READY)
        pipeline["providers"].llm.complete = lambda s, u, *, schema=None: ScenePrompts(
            cover="a portrait", inline=[f"scene {i}" for i in range(9)]
        )

        pipeline["run"](State.FACTCHECKED)

        rows = assets_of(pipeline["conn"], pipeline["post_id"])
        assert len(rows) == 4, "обрезка по inline_count не сработала"
        assert sum(1 for row in rows if row["kind"] == "inline") == 3

    def test_fewer_scenes_than_requested_does_not_crash(self, pipeline):
        """Провайдер вернул меньше сцен, чем просили — пост должен доехать."""
        from factory.providers.base import ScenePrompts

        pipeline["advance_through"](State.QUEUED, State.TEXT_READY)
        pipeline["providers"].llm.complete = lambda s, u, *, schema=None: ScenePrompts(
            cover="a portrait", inline=["only one scene"]
        )

        result, _ = pipeline["run"](State.FACTCHECKED)

        assert result.advanced
        assert len(assets_of(pipeline["conn"], pipeline["post_id"])) == 2

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

    def test_files_land_in_the_directory_of_their_variant(self, pipeline):
        """По папке на вариант: иначе новая генерация затрёт предыдущую.

        Затёртый вариант нельзя ни показать, ни опубликовать — а ради выбора
        между вариантами всё и делалось.
        """
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        pipeline["run"](State.PROMPTS_READY)

        expected = paths.post_tmp_dir(pipeline["post_id"], 1)
        for row in assets_of(pipeline["conn"], pipeline["post_id"]):
            assert Path(row["local_path"]).parent == expected

    def test_a_new_variant_does_not_overwrite_the_old_files(self, pipeline):
        conn, post_id = pipeline["conn"], pipeline["post_id"]
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        pipeline["run"](State.PROMPTS_READY)
        first = [row["local_path"] for row in assets_of(conn, post_id)]

        with db.write_transaction(conn):
            conn.execute("UPDATE posts SET version = 2 WHERE id = ?", (post_id,))
            conn.execute("UPDATE assets SET local_path = NULL WHERE post_id = ?", (post_id,))
        pipeline["run"](State.PROMPTS_READY)

        second = [row["local_path"] for row in assets_of(conn, post_id)]
        assert set(first) & set(second) == set(), "новый вариант лёг поверх старого"
        assert all(Path(path).is_file() for path in first), "файлы первого варианта пропали"

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

    def test_size_is_requested_explicitly_not_left_to_the_provider(self, pipeline):
        """1080×1350 задаёт шаг, а не умолчание провайдера.

        Боевой провайдер Этапа 4 придёт со своим дефолтом, и если шаг размер не
        просит, картинки молча поменяют пропорции — в ленте ВК это сразу видно.
        """
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        seen: list[dict] = []
        provider = pipeline["providers"].images
        original = provider.generate

        def capture(prompt, **kwargs):
            seen.append(kwargs)
            return original(prompt, **kwargs)

        provider.generate = capture

        pipeline["run"](State.PROMPTS_READY)

        assert seen, "провайдер картинок не вызывался"
        for call in seen:
            assert call["width"] == 1080
            assert call["height"] == 1350

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

    def test_parallel_pool_really_uses_several_workers(self, pipeline, monkeypatch):
        """FACTORY_MAX_PARALLEL_IMAGES=4 на сервере, 1 на Pi.

        Проверяются именно потоки: раньше тест смотрел только на наличие файлов и
        проходил бы при полностью выключенной параллельности.
        """
        import threading

        monkeypatch.setenv("FACTORY_MAX_PARALLEL_IMAGES", "4")
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)

        threads: set[int] = set()
        provider = pipeline["providers"].images
        original = provider.generate

        def note_thread(prompt, **kwargs):
            threads.add(threading.get_ident())
            return original(prompt, **kwargs)

        provider.generate = note_thread

        result, _ = pipeline["run"](State.PROMPTS_READY)

        assert result.advanced
        assert len(threads) > 1, "картинки сгенерированы в один поток, пул не задействован"
        assert threading.get_ident() not in threads, "генерация шла в основном потоке"

    def test_sequential_and_parallel_give_the_same_files(self, pipeline, monkeypatch):
        """Результат не должен зависеть от числа воркеров: Pi и сервер выдают одно и то же."""
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        monkeypatch.setenv("FACTORY_MAX_PARALLEL_IMAGES", "1")
        pipeline["run"](State.PROMPTS_READY)
        rows = assets_of(pipeline["conn"], pipeline["post_id"])
        sequential = {row["position"]: Path(row["local_path"]).read_bytes() for row in rows}

        with db.write_transaction(pipeline["conn"]):
            pipeline["conn"].execute(
                "UPDATE assets SET local_path = NULL WHERE post_id = ?", (pipeline["post_id"],)
            )
        for row in rows:
            Path(row["local_path"]).unlink()

        monkeypatch.setenv("FACTORY_MAX_PARALLEL_IMAGES", "4")
        pipeline["run"](State.PROMPTS_READY)
        parallel = {
            row["position"]: Path(row["local_path"]).read_bytes()
            for row in assets_of(pipeline["conn"], pipeline["post_id"])
        }

        assert sequential == parallel

    def test_a_failure_partway_keeps_the_images_already_paid_for(self, pipeline, monkeypatch):
        """Сбой на четвёртой картинке не должен обнулять первые три.

        Раньше пути писались одной транзакцией в конце шага, поэтому падение на
        последней теряло оплату за все предыдущие — а ретрай внутри tracked_call
        тут же оплачивал их заново.
        """
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        monkeypatch.setenv("FACTORY_MAX_PARALLEL_IMAGES", "1")

        provider = pipeline["providers"].images
        original = provider.generate
        counter = {"calls": 0, "fail_at": 4}

        def counted(prompt, **kwargs):
            counter["calls"] += 1
            if counter["calls"] == counter["fail_at"]:
                raise RuntimeError("провайдер отвалился на последней картинке")
            return original(prompt, **kwargs)

        provider.generate = counted

        with pytest.raises(RuntimeError):
            pipeline["run"](State.PROMPTS_READY)

        saved = [row for row in assets_of(pipeline["conn"], pipeline["post_id"]) if row["local_path"]]
        assert len(saved) == 3, "оплаченные картинки не зафиксированы в базе"
        assert counter["calls"] == 4

        counter["fail_at"] = None
        result, _ = pipeline["run"](State.PROMPTS_READY)

        assert result.advanced
        assert counter["calls"] == 5, (
            f"после сбоя сделано {counter['calls'] - 4} вызовов вместо одного — "
            "уже оплаченные картинки сгенерированы заново"
        )


class TestCompose:
    def test_advances_when_everything_is_in_place(self, pipeline):
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )
        result, _ = pipeline["run"](State.IMAGES_READY)

        assert result.next_state == State.COMPOSED

    def test_cover_file_is_replaced_by_the_composed_one(self, pipeline):
        """Шаг обязан собрать обложку, а не просто проверить, что файл на месте."""
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )
        cover_row = assets_of(pipeline["conn"], pipeline["post_id"])[0]
        before = Path(cover_row["local_path"]).read_bytes()

        pipeline["run"](State.IMAGES_READY)

        after = Path(cover_row["local_path"]).read_bytes()
        assert after != before, "файл обложки не изменился — сборка не выполнялась"

    def test_the_headline_is_on_the_cover(self, pipeline):
        """Проверяется по пикселям: текст действительно нарисован на картинке."""
        import io
        import json

        from PIL import Image

        from factory.compose import cover as cover_module

        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )
        pipeline["run"](State.IMAGES_READY)

        cover_row = assets_of(pipeline["conn"], pipeline["post_id"])[0]
        image = Image.open(io.BytesIO(Path(cover_row["local_path"]).read_bytes()))
        spec = json.loads(pipeline["project"].cover_template_path.read_text(encoding="utf-8"))

        assert cover_module.text_bounds(image, cover_module.title_colours(spec)) is not None

    def test_inline_images_are_left_alone(self, pipeline):
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )
        rows = assets_of(pipeline["conn"], pipeline["post_id"])
        inline_before = {
            row["id"]: Path(row["local_path"]).read_bytes()
            for row in rows
            if row["kind"] == "inline"
        }

        pipeline["run"](State.IMAGES_READY)

        for asset_id, data in inline_before.items():
            row = pipeline["conn"].execute(
                "SELECT local_path FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
            assert Path(row["local_path"]).read_bytes() == data

    def test_repeat_run_does_not_compose_twice(self, pipeline):
        """Сборка поверх собранного обязана не выполняться.

        Сейчас повтор безвреден случайно: плашка целиком закрывает прежний
        заголовок, и байты совпадают. Стоит владельцу уменьшить плашку в
        шаблоне — и старый заголовок вылезет наружу поверх новой картинки.
        Поэтому здесь шаблон между заходами меняется: без отметки в базе второй
        заход перерисовал бы файл, и это видно.
        """
        import json

        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )
        pipeline["run"](State.IMAGES_READY)
        cover_row = assets_of(pipeline["conn"], pipeline["post_id"])[0]
        after_first = Path(cover_row["local_path"]).read_bytes()

        template_path = pipeline["project"].cover_template_path
        spec = json.loads(template_path.read_text(encoding="utf-8"))
        spec["plate"]["height"] = 160
        template_path.write_text(json.dumps(spec), encoding="utf-8")

        result, _ = pipeline["run"](State.IMAGES_READY)

        assert result.advanced
        assert Path(cover_row["local_path"]).read_bytes() == after_first, (
            "обложка пересобрана поверх уже собранной"
        )

    def test_composed_cover_is_marked_in_the_database(self, pipeline):
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )
        pipeline["run"](State.IMAGES_READY)

        cover_row = assets_of(pipeline["conn"], pipeline["post_id"])[0]
        assert cover_row["external_ref"] == "composed"

    def test_missing_cover_file_is_reported_understandably(self, pipeline):
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )
        cover = assets_of(pipeline["conn"], pipeline["post_id"])[0]
        Path(cover["local_path"]).unlink()

        with pytest.raises(FactoryError) as excinfo:
            pipeline["run"](State.IMAGES_READY)

        assert "factory post retry" in str(excinfo.value)

    def test_no_cover_row_at_all_is_reported_understandably(self, pipeline):
        """Без этой ветки будет TypeError вместо инструкции."""
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )
        with db.write_transaction(pipeline["conn"]):
            pipeline["conn"].execute(
                "DELETE FROM assets WHERE post_id = ? AND kind = 'cover'", (pipeline["post_id"],)
            )

        with pytest.raises(FactoryError) as excinfo:
            pipeline["run"](State.IMAGES_READY)

        assert "нет обложки" in str(excinfo.value)
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


class TestCostIsRecorded:
    """Стоимость вызовов обязана попадать в runs.

    Дыра, прожившая до Этапа 3: tracked_call читал цену с того, что вернул шаг,
    а шаг возвращает StepResult — цена оставалась внутри, на ответе провайдера.
    Пока провайдеры были заглушками с нулевой ценой, заметить было нечем.
    """

    def _priced(self, pipeline, provider_name, value):
        from factory.core.retry import with_cost

        provider = getattr(pipeline["providers"], provider_name)
        original = provider.complete

        def priced(system, user, *, schema=None):
            return with_cost(original(system, user, schema=schema), value)

        provider.complete = priced

    def _runs(self, pipeline):
        return pipeline["conn"].execute(
            "SELECT step, cost_usd FROM runs ORDER BY id"
        ).fetchall()

    def test_text_step_records_what_it_spent(self, pipeline):
        self._priced(pipeline, "llm", 0.0123)

        pipeline["run"](State.QUEUED)

        rows = self._runs(pipeline)
        assert rows[-1]["cost_usd"] == pytest.approx(0.0123)

    def test_factcheck_step_records_what_it_spent(self, pipeline):
        pipeline["advance_through"](State.QUEUED)
        self._priced(pipeline, "factcheck", 0.0456)

        pipeline["run"](State.TEXT_READY)

        assert self._runs(pipeline)[-1]["cost_usd"] == pytest.approx(0.0456)

    def test_prompts_step_records_what_it_spent(self, pipeline):
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY)
        self._priced(pipeline, "llm", 0.0078)

        pipeline["run"](State.FACTCHECKED)

        assert self._runs(pipeline)[-1]["cost_usd"] == pytest.approx(0.0078)

    def _priced_images(self, pipeline, value):
        """Заглушка картинок с ценой: настоящая берёт её из ответа провайдера."""
        from factory.core.retry import with_cost

        provider = pipeline["providers"].images
        original = provider.generate

        def priced(prompt, **kwargs):
            return with_cost(_Bytes(original(prompt, **kwargs)), value)

        provider.generate = priced

    def test_images_step_records_what_it_spent(self, pipeline):
        """Картинки — почти вся цена поста, и до Этапа 4 она не считалась вовсе.

        Текст стоит 0.14, четыре картинки — 6.7. Пропущенная здесь цена
        занижала отчёт о тратах в сорок раз и ослепляла потолок расходов ровно
        к тому, ради чего он заведён. Поймано на живом посте: в runs стояло
        0.16 при реально потраченных 6.9.
        """
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        self._priced_images(pipeline, 1.68)

        pipeline["run"](State.PROMPTS_READY)

        images = 1 + pipeline["project"].image.inline_count
        assert self._runs(pipeline)[-1]["cost_usd"] == pytest.approx(1.68 * images)

    def test_already_generated_images_are_not_charged_again(self, pipeline):
        """Повтор шага не должен приписывать цену тому, что уже на диске."""
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY, State.FACTCHECKED)
        self._priced_images(pipeline, 1.68)
        pipeline["run"](State.PROMPTS_READY)

        pipeline["run"](State.PROMPTS_READY)

        assert self._runs(pipeline)[-1]["cost_usd"] is None

    def test_steps_without_provider_calls_record_nothing(self, pipeline):
        """Ноль вместо неизвестности занизил бы отчёт о тратах."""
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED, State.PROMPTS_READY
        )

        pipeline["run"](State.IMAGES_READY)

        assert self._runs(pipeline)[-1]["cost_usd"] is None

    def test_total_spending_can_be_summed_per_post(self, pipeline):
        """На этом строится и factory stats, и лимит стоимости поста."""
        self._priced(pipeline, "llm", 0.01)
        pipeline["run"](State.QUEUED)
        self._priced(pipeline, "factcheck", 0.02)
        pipeline["run"](State.TEXT_READY)

        total = pipeline["conn"].execute(
            "SELECT SUM(cost_usd) FROM runs WHERE post_id = ?", (pipeline["post_id"],)
        ).fetchone()[0]
        assert total == pytest.approx(0.03)
