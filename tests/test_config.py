"""Конфигурация проектов.

Главное требование к этому модулю — не корректность как таковая, а качество
сообщений об ошибках. Конфиг правит человек, который не читает код, и питоновский
трейсбек в ответ на пропущенное двоеточие — это тупик.
"""

from datetime import time
from pathlib import Path

import pytest
import yaml

from factory.core import config, paths
from factory.core.errors import ConfigError


def rewrite(project: Path, mutate) -> None:
    """Читает config.yaml, отдаёт словарь на правку, пишет обратно."""
    path = project / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


class TestDemoProject:
    def test_it_loads(self, demo_project):
        cfg = config.load_project("demo")

        assert cfg.slug == "demo"
        assert cfg.limits.posts_per_day == 2
        assert cfg.limits.queue_buffer == 6
        assert cfg.review.mode == "auto"
        assert cfg.image.inline_count == 3

    def test_every_provider_is_a_stub(self, demo_project):
        """Иначе первый же запуск полезет наружу и потратит деньги."""
        cfg = config.load_project("demo")

        assert cfg.llm.provider == "stub"
        assert cfg.image.provider == "stub"
        assert cfg.publisher.provider == "stub"

    def test_queue_buffer_follows_the_rule_of_thumb(self, demo_project):
        cfg = config.load_project("demo")
        assert cfg.limits.queue_buffer == cfg.limits.posts_per_day * 3

    def test_referenced_files_exist(self, demo_project):
        cfg = config.load_project("demo")

        assert cfg.voice_path.is_file()
        assert cfg.cover_template_path.is_file()
        assert len(cfg.style_examples()) == 2
        assert "Кристина" in cfg.voice()

    def test_prompt_files_contain_no_meta_commentary(self, demo_project):
        """Эти файлы уходят в нейросеть дословно.

        Пояснение вида «Пример поста для few-shot» модель прочитает как часть
        задания и начнёт писать про примеры вместо постов. Объяснения для
        человека живут в projects/<slug>/README.md.
        """
        cfg = config.load_project("demo")
        forbidden = ["few-shot", "Этап", "LLM", "провайдер", "config.yaml", "заглушк"]

        texts = {"voice.md": cfg.voice()}
        for index, example in enumerate(cfg.style_examples(), 1):
            texts[f"example_{index}.md"] = example

        for name, text in texts.items():
            for marker in forbidden:
                assert marker not in text, f"в {name} попал служебный текст: «{marker}»"

    def test_examples_can_be_switched_off_by_renaming(self, demo_project):
        """RUNBOOK обещает, что .md.off выключает пример без удаления файла."""
        example = demo_project / "prompts" / "examples" / "example_1.md"
        example.rename(example.with_suffix(".md.off"))

        assert len(config.load_project("demo").style_examples()) == 1

    def test_schedule_is_parsed_into_times(self, demo_project):
        cfg = config.load_project("demo")

        assert cfg.vk.slots == [time(19, 30), time(21, 0)]
        assert str(cfg.vk.tz) == "Europe/Moscow"


class TestMissingProject:
    def test_unknown_slug_lists_what_is_available(self, demo_project):
        with pytest.raises(ConfigError) as excinfo:
            config.load_project("auto_girl")

        message = str(excinfo.value)
        assert "auto_girl" in message
        assert "demo" in message
        assert "FACTORY_PROJECTS_DIR" in message

    def test_empty_projects_dir_says_so(self, tmp_env):
        with pytest.raises(ConfigError, match="Ни одного проекта не найдено"):
            config.load_project("demo")


class TestValidationMessages:
    def test_missing_section_names_the_file_and_the_field(self, demo_project):
        rewrite(demo_project, lambda data: data.pop("vk"))

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        message = str(excinfo.value)
        assert "config.yaml" in message
        assert "vk" in message
        assert "не хватает обязательного поля" in message
        assert "Что делать:" in message

    def test_typo_in_a_field_name_is_caught(self, demo_project):
        """Молча проигнорированная опечатка — худший вид ошибки конфига."""

        def mutate(data):
            data["limits"]["posts_per_days"] = data["limits"].pop("posts_per_day")

        rewrite(demo_project, mutate)

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        assert "posts_per_days" in str(excinfo.value)
        assert "опечатка" in str(excinfo.value)

    def test_unknown_provider_lists_the_available_ones(self, demo_project):
        rewrite(demo_project, lambda data: data["llm"].update(provider="gigachat"))

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        message = str(excinfo.value)
        assert "llm.provider" in message
        for name in config.LLM_PROVIDERS:
            assert name in message

    def test_broken_time_in_schedule(self, demo_project):
        rewrite(demo_project, lambda data: data["vk"].update(schedule=["25:00"]))

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        assert "ЧЧ:ММ" in str(excinfo.value)

    def test_unknown_timezone(self, demo_project):
        rewrite(demo_project, lambda data: data["vk"].update(timezone="Europe/Москва"))

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        assert "Europe/Moscow" in str(excinfo.value)

    def test_buffer_smaller_than_daily_limit_is_refused(self, demo_project):
        """С таким конфигом система физически не выпустит posts_per_day постов."""

        def mutate(data):
            data["limits"]["posts_per_day"] = 5
            data["limits"]["queue_buffer"] = 2

        rewrite(demo_project, mutate)

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        message = str(excinfo.value)
        assert "queue_buffer" in message
        assert "15" in message, "в подсказке должно быть рекомендуемое значение"

    def test_slug_mismatch_between_file_and_directory(self, demo_project):
        rewrite(demo_project, lambda data: data.update(slug="other"))

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        assert "idem_key" in str(excinfo.value)

    def test_broken_yaml_points_at_the_syntax(self, demo_project):
        (demo_project / "config.yaml").write_text("slug: demo\n  vk:\n", encoding="utf-8")

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        assert "не читается как YAML" in str(excinfo.value)
        assert "отступ" in str(excinfo.value)

    def test_empty_file_says_so(self, demo_project):
        (demo_project / "config.yaml").write_text("", encoding="utf-8")

        with pytest.raises(ConfigError, match="пустой"):
            config.load_project("demo")

    def test_wrong_choice_lists_the_allowed_values_in_russian(self, demo_project):
        rewrite(demo_project, lambda data: data["review"].update(mode="email"))

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        message = str(excinfo.value)
        assert "допустимые значения" in message
        assert "telegram" in message and "auto" in message
        assert "Input should be" not in message, "сообщение pydantic утекло непереведённым"

    def test_non_numeric_value_is_explained_in_russian(self, demo_project):
        rewrite(demo_project, lambda data: data["limits"].update(posts_per_day="две"))

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        message = str(excinfo.value)
        assert "должно быть целым числом" in message
        assert "Input should be" not in message

    def test_out_of_range_value_is_explained_in_russian(self, demo_project):
        rewrite(demo_project, lambda data: data["image"].update(inline_count=42))

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        message = str(excinfo.value)
        assert "не больше 9" in message
        assert "Input should be" not in message

    def test_all_problems_are_reported_at_once(self, demo_project):
        """Чинить конфиг по одной ошибке за запуск — издевательство."""

        def mutate(data):
            data.pop("persona")
            data["review"]["mode"] = "email"

        rewrite(demo_project, mutate)

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        message = str(excinfo.value)
        assert "persona" in message
        assert "review.mode" in message
        assert "Найдено проблем: 2" in message


class TestMissingFiles:
    def test_missing_voice_file_is_caught_at_startup(self, demo_project):
        (demo_project / "prompts" / "voice.md").unlink()

        with pytest.raises(ConfigError) as excinfo:
            config.load_project("demo")

        assert "голосом персонажа" in str(excinfo.value)

    def test_missing_cover_template_is_caught_at_startup(self, demo_project):
        (demo_project / "templates" / "red_frame.json").unlink()

        with pytest.raises(ConfigError, match="шаблон обложки"):
            config.load_project("demo")


class TestSecrets:
    def test_missing_secret_says_exactly_what_to_add(self, monkeypatch):
        monkeypatch.delenv("VK_TOKEN_DEMO", raising=False)

        with pytest.raises(ConfigError) as excinfo:
            config.resolve_secret("VK_TOKEN_DEMO", context="публикации в группу demo")

        message = str(excinfo.value)
        assert "VK_TOKEN_DEMO" in message
        assert str(paths.env_file()) in message
        assert "RUNBOOK.md" in message

    def test_empty_value_counts_as_missing(self, monkeypatch):
        monkeypatch.setenv("VK_TOKEN_DEMO", "")

        with pytest.raises(ConfigError):
            config.resolve_secret("VK_TOKEN_DEMO", context="публикации")

    def test_present_secret_is_returned(self, monkeypatch):
        monkeypatch.setenv("VK_TOKEN_DEMO", "vk1-abc")

        assert config.resolve_secret("VK_TOKEN_DEMO", context="публикации") == "vk1-abc"


class TestEnvFile:
    def test_values_are_loaded(self, tmp_env, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        paths.env_file().write_text(
            '# комментарий\n\nLLM_API_KEY="sk-from-file"\nBROKEN LINE\n', encoding="utf-8"
        )

        assert config.load_env_file() == 1
        assert config.resolve_secret("LLM_API_KEY", context="LLM") == "sk-from-file"

    def test_real_environment_wins(self, tmp_env, monkeypatch):
        """docker compose передаёт переменные напрямую — файл не должен их перебивать."""
        monkeypatch.setenv("LLM_API_KEY", "sk-from-env")
        paths.env_file().write_text("LLM_API_KEY=sk-from-file\n", encoding="utf-8")

        config.load_env_file()

        assert config.resolve_secret("LLM_API_KEY", context="LLM") == "sk-from-env"

    def test_missing_file_is_not_an_error(self, tmp_env):
        assert config.load_env_file() == 0
