"""Запись настроек проекта из панели.

Самое опасное место: сломанный конфиг кладёт проект целиком, а вместе с ним и
список проверяющих. Это уже случалось живьём, поэтому проверяется здесь не
«сохранилось», а три свойства — файл не портится при отказе, комментарии живы,
проверка ровно та же, что у воркера.
"""

import io

import pytest
from PIL import Image

from factory.core import config_write
from factory.core.config import load_project
from factory.core.errors import ConfigError


@pytest.fixture
def project(demo_project):
    return demo_project


def read(project) -> str:
    return (project / "config.yaml").read_text(encoding="utf-8")


class TestValidation:
    def test_a_good_change_is_written(self, project):
        config_write.update("demo", {"limits": {"posts_per_day": 3, "queue_buffer": 9}})

        assert load_project("demo").limits.posts_per_day == 3

    def test_a_bad_value_does_not_touch_the_file(self, project):
        """Отказ вместо порчи. Проверяется по файлу, а не по исключению.

        Записать и потом откатить — не то же самое: воркер читает конфиг каждый
        проход и успел бы прочитать сломанный.
        """
        before = read(project)

        with pytest.raises(ConfigError):
            config_write.update("demo", {"limits": {"posts_per_day": -5}})

        assert read(project) == before

    def test_the_error_is_the_same_one_the_worker_would_show(self, project):
        """Панель не имеет права быть снисходительнее воркера."""
        with pytest.raises(ConfigError) as exc:
            config_write.update("demo", {"llm": {"max_tokens": "много"}})

        assert "max_tokens" in str(exc.value)

    def test_a_broken_cross_field_rule_is_caught(self, project):
        """Правила, связывающие поля, тоже обязаны проверяться.

        queue_buffer меньше posts_per_day означает, что система физически не
        выпустит столько постов, сколько ей велено, — и молчать об этом нельзя.
        """
        with pytest.raises(ConfigError):
            config_write.update("demo", {"limits": {"posts_per_day": 9, "queue_buffer": 2}})

    def test_the_slug_cannot_be_changed(self, project):
        with pytest.raises(ConfigError) as exc:
            config_write.update("demo", {"slug": "другой"})

        assert "менять нельзя" in str(exc.value)


class TestComments:
    def test_comments_survive_a_save(self, project):
        """Половина конфига — пояснения владельцу, писавшиеся месяц.

        Обычная запись YAML стирает их за один раз, и файл превращается в
        голые значения, по которым уже ничего не понять.
        """
        before = read(project)
        comments = [line for line in before.splitlines() if line.strip().startswith("#")]
        assert comments, "в образце конфига нет комментариев — тест бессмыслен"

        config_write.update("demo", {"limits": {"posts_per_day": 3, "queue_buffer": 9}})

        after = read(project)
        for line in comments:
            assert line in after, f"пропал комментарий: {line.strip()[:60]}"

    def test_untouched_sections_stay_as_they_were(self, project):
        """Панель присылает один раздел, а в файле их девять."""
        before = load_project("demo")

        config_write.update("demo", {"limits": {"posts_per_day": 3, "queue_buffer": 9}})

        after = load_project("demo")
        assert after.llm.model == before.llm.model
        assert after.image.scene_style == before.image.scene_style
        assert after.content.target_length == before.content.target_length

    def test_a_nested_field_does_not_erase_its_neighbours(self, project):
        before = load_project("demo")

        config_write.update("demo", {"image": {"inline_count": 2}})

        after = load_project("demo")
        assert after.image.inline_count == 2
        assert after.image.cover_template == before.image.cover_template


class TestAtomicity:
    def test_no_leftovers_after_a_save(self, project):
        config_write.update("demo", {"limits": {"posts_per_day": 3, "queue_buffer": 9}})

        leftovers = [item.name for item in project.iterdir() if item.name.startswith(".config")]
        assert leftovers == []

    def test_the_file_mode_is_preserved(self, project):
        """Права переносятся со старого файла на новый.

        Проверяется на 0644, а не на 0600: временный файл и так создаётся с
        правами 0600, и тест на них проходил бы даже без переноса — то есть
        не проверял бы ничего.
        """
        path = project / "config.yaml"
        path.chmod(0o644)

        config_write.update("demo", {"limits": {"posts_per_day": 3, "queue_buffer": 9}})

        assert path.stat().st_mode & 0o777 == 0o644

    def test_a_failed_write_leaves_no_half_file(self, project, monkeypatch):
        """Воркер читает конфиг каждый проход и не должен прочитать половину.

        Обрыв посередине обязан оставить либо старую версию, либо новую.
        """
        path = project / "config.yaml"
        before = path.read_text(encoding="utf-8")

        def boom(*args, **kwargs):
            raise OSError("диск кончился")

        monkeypatch.setattr(config_write.os, "replace", boom)

        with pytest.raises(OSError):
            config_write.update("demo", {"limits": {"posts_per_day": 3, "queue_buffer": 9}})

        assert path.read_text(encoding="utf-8") == before
        assert [item.name for item in project.iterdir() if item.name.startswith(".config")] == []


class TestProjectFiles:
    def test_a_prompt_can_be_rewritten(self, project):
        config_write.write_text_file("demo", "prompts/voice.md", "Ты — другой персонаж.")

        assert load_project("demo").voice() == "Ты — другой персонаж."

    def test_a_path_outside_the_project_is_refused(self, project):
        """Имя файла приходит из запроса, а панель пишет правами воркера.

        Без проверки «../../data/.env» перезаписал бы файл секретов.
        """
        with pytest.raises(ConfigError) as exc:
            config_write.write_text_file("demo", "../../data/.env", "VK_TOKEN_GROUP=чужой")

        assert "не принадлежит проекту" in str(exc.value)

    def test_a_binary_file_can_be_replaced(self, project):
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), (10, 20, 30)).save(buffer, format="PNG")

        config_write.write_bytes_file("demo", "character/canon.png", buffer.getvalue())

        assert (project / "character" / "canon.png").is_file()

    def test_a_binary_path_outside_the_project_is_refused(self, project):
        with pytest.raises(ConfigError):
            config_write.write_bytes_file("demo", "../../data/.env", b"\x00")
