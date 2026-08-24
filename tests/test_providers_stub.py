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

    def test_title_comes_from_the_topic(self):
        """Заглушка не знает тематику проекта и брать заголовок ей больше неоткуда.

        Захардкоженные заголовки означали бы знание о нише внутри providers/ —
        спека это прямо запрещает. Побочная выгода: посты в демо-прогоне
        отличаются друг от друга, и видно, какой пост про что.
        """
        draft = StubLLM().complete(
            "система", "Тема: Как выбрать шины на зиму\nОбъём: 900-1400", schema=PostDraft
        )

        assert draft.title == "Как выбрать шины на зиму"
        assert "Как выбрать шины на зиму" in draft.body

    def test_different_topics_give_different_posts(self):
        titles = {
            StubLLM().complete("система", f"Тема: тема номер {i}", schema=PostDraft).title
            for i in range(20)
        }
        assert len(titles) == 20, "заглушка выдаёт один и тот же текст на разные темы"

    def test_long_topic_is_trimmed_to_fit_the_cover(self):
        draft = StubLLM().complete("система", f"Тема: {'о' * 200}", schema=PostDraft)

        assert len(draft.title) <= 60

    def test_request_without_a_topic_does_not_crash(self):
        assert StubLLM().complete("система", "напиши что-нибудь", schema=PostDraft).title

    def test_no_niche_specific_content_is_baked_in(self):
        """В core/ и providers/ не должно быть ничего про тематику сообщества.

        Сторож грубый: он ловит русские существительные в строковых литералах, а
        не понимает смысл. Кулинарную или медицинскую специфику он поймает по
        тому же принципу — длинные русские слова внутри кавычек в providers/
        почти всегда означают протёкшую тематику, — но гарантией это не является.
        Настоящая проверка — пункт 5 чеклиста самокритики в CLAUDE.md.
        """
        import re
        from pathlib import Path

        package = Path(__file__).resolve().parent.parent / "factory"
        literal = re.compile(r'"[^"]*"|\'[^\']*\'')
        russian_word = re.compile(r"[а-яё\-]+", re.IGNORECASE)

        # Корни слов из разных ниш. Сравнение идёт по началу слова, а не по
        # подстроке: иначе «стейт-машине» ловилось бы корнем «шин».
        niche_stems = (
            "шин", "светофор", "бензин", "двигател", "автомоб", "багажник",
            "рецепт", "блюд", "духовк", "диагноз", "симптом", "лекарств",
            "тренировк", "гантел",
        )

        offenders: list[str] = []
        for path in sorted(package.rglob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for chunk in literal.findall(line):
                    for word in russian_word.findall(chunk):
                        # «стейт-машине» разбивается на части по дефису.
                        for part in word.lower().split("-"):
                            if part.startswith(niche_stems):
                                offenders.append(
                                    f"{path.relative_to(package)}:{lineno} «{word}»"
                                )

        assert not offenders, "в код протекла тематика ниши: " + "; ".join(offenders)

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


class TestLiveProviderWiring:
    """Сборка боевого провайдера из конфига.

    Каждая проверка здесь закрывает молчаливую подмену: конфиг проходит
    валидацию, владелец уверен, что фактчек идёт с поиском, а запрос уходит на
    модель для текстов — и ни один тест не краснеет.
    """

    @pytest.fixture
    def live_config(self, demo_project, monkeypatch):
        monkeypatch.setenv("LLM_BASE_URL", "https://router.example/v1")
        monkeypatch.setenv("ROUTER_KEY", "sk-test")
        cfg = load_project("demo")
        return cfg.model_copy(
            update={
                "llm": cfg.llm.model_copy(
                    update={
                        "provider": "openai_compatible",
                        "base_url_env": "LLM_BASE_URL",
                        "api_key_env": "ROUTER_KEY",
                        "model": "vendor/writer",
                        "factcheck_model": "vendor/searcher",
                        "factcheck_web_search": True,
                        "max_tokens": 1234,
                        "temperature": 0.3,
                        "price_input_per_1m": 10.0,
                        "price_output_per_1m": 20.0,
                        "factcheck_price_input_per_1m": 30.0,
                        "factcheck_price_output_per_1m": 40.0,
                    }
                )
            }
        )

    def test_factcheck_gets_its_own_model(self, live_config):
        providers = registry.build_providers(live_config)

        assert providers.llm.model == "vendor/writer"
        assert providers.factcheck.model == "vendor/searcher"

    def test_factcheck_falls_back_to_the_main_model_when_unset(self, live_config):
        cfg = live_config.model_copy(
            update={"llm": live_config.llm.model_copy(update={"factcheck_model": None})}
        )

        providers = registry.build_providers(cfg)

        assert providers.factcheck.model == "vendor/writer"

    def test_prices_do_not_get_swapped(self, live_config):
        """Перепутанные местами цены дают правдоподобный, но неверный отчёт."""
        providers = registry.build_providers(live_config)

        assert providers.llm.price_input_per_1m == 10.0
        assert providers.llm.price_output_per_1m == 20.0
        assert providers.factcheck.price_input_per_1m == 30.0
        assert providers.factcheck.price_output_per_1m == 40.0

    def test_token_limit_and_temperature_come_from_the_config(self, live_config):
        providers = registry.build_providers(live_config)

        assert providers.llm.max_tokens == 1234
        assert providers.llm.temperature == 0.3
        assert providers.factcheck.max_tokens == 1234

    def test_proxy_and_key_variable_names_reach_the_provider(self, live_config):
        cfg = live_config.model_copy(
            update={"llm": live_config.llm.model_copy(update={"proxy_env": "LLM_PROXY"})}
        )

        providers = registry.build_providers(cfg)

        assert providers.llm.proxy_env == "LLM_PROXY"
        # Имя переменной нужно, чтобы совет в ошибке 401 указывал на настоящую
        # строку в файле секретов, а не на выдуманную. Имя здесь намеренно не
        # совпадает с запасным значением в коде провайдера.
        assert providers.llm.key_env == "ROUTER_KEY"
