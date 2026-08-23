"""Протоколы провайдеров и заглушки."""

import io
import json

import pytest
from PIL import Image

from factory.core import config as config_module
from factory.core.config import load_project
from factory.core.errors import ConfigError
from factory.providers import registry
from factory.providers.base import (
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    TITLE_MAX_LENGTH,
    FactcheckResult,
    ImageProvider,
    LLMProvider,
    PostDraft,
    Publisher,
    ScenePrompts,
)
from factory.providers.images.stub import StubImages
from factory.providers.llm.stub import StubLLM
from factory.providers.publishers.stub import StubPublisher


class TestRegistry:
    def test_demo_project_gets_three_stubs(self, demo_project):
        providers = registry.build_providers(load_project("demo"))

        assert isinstance(providers.llm, StubLLM)
        assert isinstance(providers.images, StubImages)
        assert isinstance(providers.publisher, StubPublisher)

    def test_stubs_satisfy_the_protocols(self, demo_project):
        providers = registry.build_providers(load_project("demo"))

        assert isinstance(providers.llm, LLMProvider)
        assert isinstance(providers.images, ImageProvider)
        assert isinstance(providers.publisher, Publisher)

    def test_config_settings_reach_the_provider(self, demo_project):
        providers = registry.build_providers(load_project("demo"))

        assert providers.llm.model == "stub"
        assert providers.publisher.group_id == 0

    def test_not_yet_implemented_provider_says_which_stage(self, demo_project):
        cfg = load_project("demo")
        cfg = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"provider": "anthropic"})})

        with pytest.raises(ConfigError) as excinfo:
            registry.build_providers(cfg)

        message = str(excinfo.value)
        assert "Этапе 3" in message
        assert "stub" in message

    def test_names_accepted_by_the_config_all_have_an_entry(self):
        """Иначе конфиг проходит валидацию, а падает уже в бою при сборке провайдеров.

        Реализации может ещё не быть — но тогда должно быть понятное сообщение
        про этап, а не KeyError.
        """
        pairs = [
            (config_module.LLM_PROVIDERS, registry.LLM_BUILDERS),
            (config_module.IMAGE_PROVIDERS, registry.IMAGE_BUILDERS),
            (config_module.PUBLISHER_PROVIDERS, registry.PUBLISHER_BUILDERS),
        ]
        for allowed, builders in pairs:
            for name in allowed:
                assert name in builders or name in registry._STAGE_OF, (
                    f"провайдер '{name}' разрешён конфигом, но реестр про него ничего не знает"
                )

    def test_builders_do_not_offer_names_the_config_rejects(self):
        assert set(registry.LLM_BUILDERS) <= set(config_module.LLM_PROVIDERS)
        assert set(registry.IMAGE_BUILDERS) <= set(config_module.IMAGE_PROVIDERS)
        assert set(registry.PUBLISHER_BUILDERS) <= set(config_module.PUBLISHER_PROVIDERS)


class TestStubLLM:
    def test_post_draft_has_every_field(self):
        draft = StubLLM().complete("система", "напиши пост", schema=PostDraft)

        assert isinstance(draft, PostDraft)
        assert draft.title and draft.body and draft.question

    def test_title_fits_the_cover(self):
        """Спека: максимум 60 символов, без точки в конце.

        Числа записаны литералами намеренно. Сравнение с TITLE_MAX_LENGTH было бы
        тавтологией: правка константы меняла бы обе стороны равенства, и тест
        молча перестал бы защищать требование спеки.
        """
        draft = StubLLM().complete("система", "напиши пост", schema=PostDraft)

        assert TITLE_MAX_LENGTH == 60
        assert len(draft.title) <= 60
        assert not draft.title.endswith(".")

    def test_trailing_period_is_stripped_by_the_schema(self):
        """Проверяем сам контракт, а не заглушку: боевой провайдер точку пришлёт."""
        assert PostDraft(title="Заголовок.", body="текст", question="а?").title == "Заголовок"

    def test_too_long_title_is_rejected(self):
        with pytest.raises(ValueError):
            PostDraft(title="а" * (TITLE_MAX_LENGTH + 1), body="текст", question="а?")

    def test_factcheck_result_is_returned(self):
        result = StubLLM().complete("система", "проверь", schema=FactcheckResult)

        assert isinstance(result, FactcheckResult)
        assert result.verdict == "ok"

    def test_factcheck_verdict_is_constrained(self):
        with pytest.raises(ValueError):
            FactcheckResult(verdict="наверное")

    def test_scene_prompts_are_in_english(self):
        """Промпты картинок — английские: модели рисуют по ним заметно лучше."""
        prompts = StubLLM().complete("система", "сцены", schema=ScenePrompts)

        assert isinstance(prompts, ScenePrompts)
        assert prompts.cover.isascii()
        assert all(scene.isascii() for scene in prompts.inline)

    def test_output_is_deterministic(self):
        """Иначе нельзя отличить «восстановлено после краша» от «сгенерено заново»."""
        first = StubLLM().complete("система", "напиши пост", schema=PostDraft)
        second = StubLLM().complete("система", "напиши пост", schema=PostDraft)

        assert first == second

    def test_different_input_gives_different_output(self):
        titles = {
            StubLLM().complete("система", f"тема {i}", schema=PostDraft).title for i in range(20)
        }
        assert len(titles) > 1, "заглушка выдаёт один и тот же текст на любой запрос"

    def test_plain_text_without_a_schema(self):
        assert isinstance(StubLLM().complete("система", "привет"), str)

    def test_calls_are_counted(self):
        """Счётчик нужен тестам идемпотентности: платный вызов не должен повторяться."""
        llm = StubLLM()
        llm.complete("s", "u")
        llm.complete("s", "u")

        assert llm.calls == 2


class TestStubImages:
    def test_returns_a_valid_png(self):
        data = StubImages().generate("a portrait")

        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert Image.open(io.BytesIO(data)).format == "PNG"

    def test_size_matches_the_spec(self):
        """Спека: 1080×1350 для всех картинок. Литералы — см. пояснение выше."""
        image = Image.open(io.BytesIO(StubImages().generate("a portrait")))

        assert (IMAGE_WIDTH, IMAGE_HEIGHT) == (1080, 1350)
        assert image.size == (1080, 1350)

    def test_custom_size_is_honoured(self):
        image = Image.open(io.BytesIO(StubImages().generate("x", width=200, height=300)))

        assert image.size == (200, 300)

    def test_same_prompt_and_seed_give_identical_bytes(self):
        first = StubImages().generate("a portrait", seed=7)
        second = StubImages().generate("a portrait", seed=7)

        assert first == second

    def test_different_prompts_give_different_images(self):
        first = StubImages().generate("a portrait", seed=7)
        second = StubImages().generate("a car", seed=7)

        assert first != second

    def test_different_seeds_give_different_images(self):
        """Кнопка «Картинки заново» шлёт тот же промпт с новым seed."""
        first = StubImages().generate("a portrait", seed=1)
        second = StubImages().generate("a portrait", seed=2)

        assert first != second

    def test_empty_prompt_does_not_crash(self):
        assert StubImages().generate("")

    def test_calls_are_counted(self):
        images = StubImages()
        images.generate("a")
        assert images.calls == 1


class TestStubPublisher:
    def test_returns_an_external_id(self, tmp_env, fake_post):
        assert StubPublisher().publish(fake_post, []) == f"stub_{fake_post.id}"

    def test_writes_a_readable_file(self, tmp_env, fake_post):
        from factory.core import paths

        StubPublisher().publish(fake_post, [])

        path = paths.tmp_dir() / "published" / f"post_{fake_post.id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))

        assert payload["title"] == fake_post.title
        assert payload["body"] == fake_post.body

    def test_attachments_are_listed(self, tmp_env, fake_post, fake_asset):
        from factory.core import paths

        StubPublisher().publish(fake_post, [fake_asset])

        path = paths.tmp_dir() / "published" / f"post_{fake_post.id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))

        assert payload["attachments"] == [
            {"kind": "cover", "position": 0, "path": "/tmp/factory/1/cover.png"}
        ]

    def test_fetch_comments_returns_nothing(self):
        assert StubPublisher().fetch_comments("stub_1") == []


@pytest.fixture
def fake_post():
    class Post:
        id = 1
        title = "Заголовок"
        body = "Текст поста"
        question = "А у вас?"

    return Post()


@pytest.fixture
def fake_asset():
    class Asset:
        kind = "cover"
        position = 0
        local_path = "/tmp/factory/1/cover.png"

    return Asset()
