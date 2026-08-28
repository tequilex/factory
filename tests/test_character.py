"""Узнаваемость персонажа: карточка примет и эталонный портрет.

Механизм проверен живыми вызовами до написания кода: карточка примет плюс один
образец держат лицо, пирсинг, серьги и стрижку на всех сценах. Здесь проверяется
не качество картинок — его проверить нечем, — а то, что карточка действительно
доезжает до модели, и что нерабочая настройка не проходит молча.
"""

import pytest
from pydantic import ValidationError

from factory.core.config import ImageCfg, load_project
from factory.core.errors import ConfigError
from factory.core.models import State
from factory.core.steps.prompts import scene_prompt


class TestPromptAssembly:
    def test_order_is_character_scene_style(self):
        """Порядок проверялся живьём именно такой.

        Приметы впереди: то, что стоит в начале промпта, модель держит крепче, а
        держать надо человека, а не свет.
        """
        assert scene_prompt("woman with a septum piercing", "checking the oil", "35mm photo") == (
            "woman with a septum piercing, checking the oil, 35mm photo"
        )

    def test_empty_character_leaves_no_stray_comma(self):
        assert scene_prompt("", "checking the oil", "35mm photo") == (
            "checking the oil, 35mm photo"
        )

    def test_empty_style_leaves_no_stray_comma(self):
        assert scene_prompt("woman", "checking the oil", "") == "woman, checking the oil"

    def test_scene_alone_survives_untouched(self):
        assert scene_prompt("", "checking the oil", "") == "checking the oil"

    def test_trailing_commas_in_config_do_not_double_up(self):
        """Владелец пишет карточку руками, и запятая в конце строки неизбежна."""
        assert scene_prompt("woman,", "checking the oil", ", 35mm") == (
            "woman, checking the oil, 35mm"
        )


class TestStepWritesTheCard:
    def _with_character(self, pipeline, character: str):
        project = pipeline["project"]
        return project.model_copy(
            update={"image": project.image.model_copy(update={"character": character})}
        )

    def _prompts(self, pipeline) -> list[str]:
        rows = pipeline["conn"].execute(
            "SELECT prompt FROM assets WHERE post_id = ? ORDER BY position",
            (pipeline["post_id"],),
        ).fetchall()
        return [row["prompt"] for row in rows]

    def test_card_reaches_every_scene(self, pipeline):
        """Обложка и все сопровождающие: персонаж один на всём посте.

        Если карточка попадёт только на обложку, три остальные картинки будут с
        другим человеком, и заметит это владелец, а не система.
        """
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY)
        ctx = pipeline["context"](State.FACTCHECKED)
        ctx.project = self._with_character(pipeline, "woman with a snake tattoo")

        from factory.core.steps import prompts

        prompts.run(ctx)

        written = self._prompts(pipeline)
        assert len(written) == 1 + pipeline["project"].image.inline_count
        assert all(prompt.startswith("woman with a snake tattoo, ") for prompt in written)

    def test_without_a_card_prompts_stay_clean(self, pipeline):
        pipeline["advance_through"](State.QUEUED, State.TEXT_READY)
        ctx = pipeline["context"](State.FACTCHECKED)
        ctx.project = self._with_character(pipeline, "")

        from factory.core.steps import prompts

        prompts.run(ctx)

        assert all(not prompt.startswith(", ") for prompt in self._prompts(pipeline))


class TestConfigGuards:
    """Настройка, которая тихо ничего не делает, хуже сломанной."""

    def _image(self, **overrides) -> dict:
        base = {
            "provider": "stub",
            "model": "stub",
            "cover_template": "templates/red_frame.json",
        }
        base.update(overrides)
        return base

    def test_reference_without_support_is_refused(self):
        """Образец, который никуда не уходит, — худший из тихих сбоев.

        Файл на месте, приметы на месте, а персонаж на каждом посте разный, и
        никакой ошибки при этом нет. Проверено живьём: gemini-2.5-flash-image
        отвечает 200, берёт деньги и рисует другого человека.
        """
        with pytest.raises(ValidationError) as exc:
            ImageCfg(**self._image(reference="character/canon.png"))

        assert "supports_reference" in str(exc.value)

    def test_reference_with_support_is_accepted(self):
        config = ImageCfg(
            **self._image(reference="character/canon.png", supports_reference=True)
        )

        assert config.reference == "character/canon.png"

    def test_real_provider_demands_key_and_address(self):
        with pytest.raises(ValidationError) as exc:
            ImageCfg(**self._image(provider="openai_compatible"))

        assert "api_key_env" in str(exc.value)
        assert "base_url_env" in str(exc.value)

    def test_missing_reference_file_is_caught_at_startup(self, demo_project):
        """Не на четвёртом платном вызове, а на загрузке конфига."""
        config_path = demo_project / "config.yaml"
        text = config_path.read_text(encoding="utf-8")
        config_path.write_text(
            text.replace(
                "  cover_template: templates/red_frame.json",
                "  cover_template: templates/red_frame.json\n"
                "  reference: character/canon.png\n"
                "  supports_reference: true",
            ),
            encoding="utf-8",
        )

        with pytest.raises(ConfigError) as exc:
            load_project("demo")

        assert "эталонный портрет" in str(exc.value)

    def test_present_reference_file_passes(self, demo_project):
        character_dir = demo_project / "character"
        character_dir.mkdir()
        (character_dir / "canon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        config_path = demo_project / "config.yaml"
        text = config_path.read_text(encoding="utf-8")
        config_path.write_text(
            text.replace(
                "  cover_template: templates/red_frame.json",
                "  cover_template: templates/red_frame.json\n"
                "  reference: character/canon.png\n"
                "  supports_reference: true",
            ),
            encoding="utf-8",
        )

        assert load_project("demo").reference_path == character_dir / "canon.png"
