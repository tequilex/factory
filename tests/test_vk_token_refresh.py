"""Обновление ключа загрузки ВК через Telegram.

Ключ живёт сутки, продлить его нельзя — это главная эксплуатационная дыра
проекта. Здесь проверяется вся цепочка: воркер заметил, позвал владельца один
раз, владелец прислал адрес из браузера, ключ подставился и подхватился без
перезапуска.
"""

import asyncio
import os
import stat
from dataclasses import dataclass, field

import pytest

from factory.core import alerts, secrets
from factory.core.config import TelegramCfg
from factory.core.errors import ConfigError
from factory.providers.notifiers.stub import StubNotifier
from factory.providers.notifiers.telegram import extract_vk_token
from factory.providers.publishers.vk import VkError

OWNER = 123456789


class TestExtractToken:
    """Владелец шлёт адрес из браузера на телефоне, а не аккуратную подстроку."""

    def test_a_whole_redirect_url(self):
        token = extract_vk_token(
            "https://oauth.vk.ru/blank.html#access_token=vk1.a.qwertyuiop1234567890AB"
            "&expires_in=0&user_id=123456789"
        )

        assert token == "vk1.a.qwertyuiop1234567890AB"

    def test_a_bare_token(self):
        assert extract_vk_token("vk1.a.qwertyuiop1234567890AB") == "vk1.a.qwertyuiop1234567890AB"

    def test_surrounding_whitespace_is_forgiven(self):
        assert extract_vk_token("  vk1.a.qwertyuiop1234567890AB \n") is not None

    @pytest.mark.parametrize(
        "text",
        [
            "привет",
            "",
            "https://oauth.vk.ru/blank.html",
            "access_token=короткий",
            "https://example.com/?access_token=notavktoken",
        ],
    )
    def test_anything_that_is_not_a_token_is_refused(self, text):
        """Случайная ссылка не должна затереть рабочий ключ."""
        assert extract_vk_token(text) is None

    def test_a_truncated_token_is_refused(self):
        """Обрезанный при копировании ключ лучше отвергнуть, чем сохранить."""
        assert extract_vk_token("vk1.a.корот") is None


class TestUpdateSecret:
    def test_the_value_is_written_and_applied(self, tmp_env, monkeypatch):
        from factory.core import paths

        monkeypatch.delenv("VK_UPLOAD_TOKEN", raising=False)
        target = paths.env_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VK_TOKEN_GROUP=старый\n", encoding="utf-8")

        secrets.update_secret("VK_UPLOAD_TOKEN", "vk1.a.новый")

        assert "VK_UPLOAD_TOKEN=vk1.a.новый" in target.read_text(encoding="utf-8")
        assert os.environ["VK_UPLOAD_TOKEN"] == "vk1.a.новый"

    def test_other_secrets_survive(self, tmp_env):
        from factory.core import paths

        target = paths.env_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VK_TOKEN_GROUP=не трогать\nLLM_API_KEY=тоже\n", encoding="utf-8")

        secrets.update_secret("VK_UPLOAD_TOKEN", "vk1.a.новый")

        text = target.read_text(encoding="utf-8")
        assert "VK_TOKEN_GROUP=не трогать" in text
        assert "LLM_API_KEY=тоже" in text

    def test_duplicates_are_collapsed(self, tmp_env):
        """Файл правит и человек, и система. Расти он не должен."""
        from factory.core import paths

        target = paths.env_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VK_UPLOAD_TOKEN=первый\nVK_UPLOAD_TOKEN=второй\n", encoding="utf-8")

        secrets.update_secret("VK_UPLOAD_TOKEN", "vk1.a.третий")

        lines = [l for l in target.read_text(encoding="utf-8").splitlines() if l.startswith("VK_UPLOAD")]
        assert lines == ["VK_UPLOAD_TOKEN=vk1.a.третий"]

    def test_the_file_stays_private(self, tmp_env):
        """В файле ключи от сообщества и от платных моделей."""
        from factory.core import paths

        secrets.update_secret("VK_UPLOAD_TOKEN", "vk1.a.новый")

        mode = stat.S_IMODE(paths.env_file().stat().st_mode)
        assert mode == 0o600, f"права {oct(mode)} — файл виден другим пользователям"

    def test_a_value_with_a_newline_is_refused(self, tmp_env):
        """Иначе одна строка файла превратится в две и сломает разбор."""
        with pytest.raises(ConfigError):
            secrets.update_secret("VK_UPLOAD_TOKEN", "vk1.a.новый\nMALICIOUS=1")

    def test_no_temporary_file_is_left_behind(self, tmp_env):
        from factory.core import paths

        secrets.update_secret("VK_UPLOAD_TOKEN", "vk1.a.новый")

        leftovers = list(paths.env_file().parent.glob(".env.*"))
        assert leftovers == []


class TestReload:
    """Вставленный ключ обязан подхватиться без перезапуска воркера."""

    def test_a_refresh_picks_up_the_new_value(self, tmp_env, monkeypatch):
        from factory.core import paths

        monkeypatch.delenv("VK_UPLOAD_TOKEN", raising=False)
        secrets._FROM_FILE.discard("VK_UPLOAD_TOKEN")
        target = paths.env_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VK_UPLOAD_TOKEN=старый\n", encoding="utf-8")
        secrets.load_env_file()
        assert os.environ["VK_UPLOAD_TOKEN"] == "старый"

        target.write_text("VK_UPLOAD_TOKEN=новый\n", encoding="utf-8")
        secrets.load_env_file(refresh=True)

        assert os.environ["VK_UPLOAD_TOKEN"] == "новый"

    def test_without_refresh_the_old_value_stays(self, tmp_env, monkeypatch):
        """Обычная загрузка не должна перетирать то, что уже в окружении."""
        from factory.core import paths

        monkeypatch.setenv("VK_UPLOAD_TOKEN", "снаружи")
        target = paths.env_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VK_UPLOAD_TOKEN=из_файла\n", encoding="utf-8")

        secrets.load_env_file()

        assert os.environ["VK_UPLOAD_TOKEN"] == "снаружи"

    def test_a_real_environment_variable_beats_the_file_even_on_refresh(
        self, tmp_env, monkeypatch
    ):
        """docker compose передаёт переменные напрямую — файл их не перебивает."""
        from factory.core import paths

        secrets._FROM_FILE.discard("VK_UPLOAD_TOKEN")
        monkeypatch.setenv("VK_UPLOAD_TOKEN", "снаружи")
        target = paths.env_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VK_UPLOAD_TOKEN=из_файла\n", encoding="utf-8")

        secrets.load_env_file(refresh=True)

        assert os.environ["VK_UPLOAD_TOKEN"] == "снаружи"


class TestAlertOnce:
    def test_the_owner_is_told(self, conn):
        notifier = StubNotifier()

        sent = alerts.raise_once(
            conn, notifier, chat_id=OWNER, name="vk_token", scope="demo", text="ключ истёк"
        )

        assert sent is True
        assert notifier.alerts == ["ключ истёк"]

    def test_the_same_alert_does_not_repeat(self, conn):
        """Тик идёт раз в минуту: повтор превратил бы тревогу в шум."""
        notifier = StubNotifier()
        alerts.raise_once(
            conn, notifier, chat_id=OWNER, name="vk_token", scope="demo", text="ключ истёк"
        )

        alerts.raise_once(
            conn, notifier, chat_id=OWNER, name="vk_token", scope="demo", text="ключ истёк"
        )

        assert len(notifier.alerts) == 1

    def test_another_project_gets_its_own_alert(self, conn):
        notifier = StubNotifier()
        alerts.raise_once(
            conn, notifier, chat_id=OWNER, name="vk_token", scope="demo", text="первый"
        )

        alerts.raise_once(
            conn, notifier, chat_id=OWNER, name="vk_token", scope="другой", text="второй"
        )

        assert len(notifier.alerts) == 2

    def test_after_clearing_it_can_sound_again(self, conn):
        """Ключ обновили, назавтра он истечёт снова — и это надо сказать."""
        notifier = StubNotifier()
        alerts.raise_once(
            conn, notifier, chat_id=OWNER, name="vk_token", scope="demo", text="ключ истёк"
        )

        alerts.clear(conn, "vk_token", "demo")
        alerts.raise_once(
            conn, notifier, chat_id=OWNER, name="vk_token", scope="demo", text="ключ истёк"
        )

        assert len(notifier.alerts) == 2

    def test_a_failed_send_does_not_silence_the_alert(self, conn):
        """Иначе сбой сети погасил бы тревогу, о которой владелец не узнал."""

        class Broken(StubNotifier):
            def alert(self, *, chat_id: int, text: str) -> None:
                raise ConfigError("сеть недоступна")

        broken = Broken()
        assert alerts.raise_once(
            conn, broken, chat_id=OWNER, name="vk_token", scope="demo", text="ключ истёк"
        ) is False

        working = StubNotifier()
        assert alerts.raise_once(
            conn, working, chat_id=OWNER, name="vk_token", scope="demo", text="ключ истёк"
        ) is True

    def test_the_message_carries_the_link_and_the_steps(self, conn):
        text = alerts.vk_token_expired_text("vk_demo", "VK_UPLOAD_TOKEN")

        assert "oauth.vk.ru/authorize" in text
        assert "VK_UPLOAD_TOKEN" in text
        assert "vk_demo" in text


class TestVkErrorCarriesTheToken:
    def test_an_expired_token_is_recognisable(self):
        error = VkError(5, "access_token has expired", method="photos.getWallUploadServer",
                        token_env="VK_UPLOAD_TOKEN")

        assert error.token_expired is True
        assert error.token_env == "VK_UPLOAD_TOKEN"

    def test_other_errors_are_not_token_problems(self):
        error = VkError(27, "group access denied", method="wall.post", token_env="VK_TOKEN_GROUP")

        assert error.token_expired is False

    def test_the_advice_names_which_key_died(self):
        """Ключа два: без имени владелец не знает, какой менять."""
        error = VkError(5, "expired", method="photos.getWallUploadServer",
                        token_env="VK_UPLOAD_TOKEN")

        assert "VK_UPLOAD_TOKEN" in str(error)


@dataclass
class FakeMessage:
    text: str
    from_user: object = None
    answered: list[str] = field(default_factory=list)
    deleted: bool = False

    async def answer(self, text: str) -> None:
        self.answered.append(text)

    async def delete(self) -> None:
        self.deleted = True


@dataclass
class FakeUser:
    id: int


class TestBotAcceptsTheToken:
    @pytest.fixture
    def env(self, pipeline, monkeypatch):
        from factory.bot import review_bot
        from factory.core import paths

        target = paths.env_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VK_UPLOAD_TOKEN=старый\n", encoding="utf-8")

        project = pipeline["project"]
        asking = project.model_copy(
            update={
                "review": project.review.model_copy(update={"mode": "telegram"}),
                "telegram": TelegramCfg(provider="stub", chat_id=OWNER, reviewers=[OWNER]),
                "vk": project.vk.model_copy(update={"upload_token_env": "VK_UPLOAD_TOKEN"}),
            }
        )
        pipeline["accept"] = lambda text, user=OWNER: _accept(
            review_bot, pipeline["conn"], {"demo": asking}, text, user
        )
        return pipeline

    def test_a_valid_token_is_saved(self, env):
        from factory.core import paths

        message = env["accept"](
            "https://oauth.vk.ru/blank.html#access_token=vk1.a.qwertyuiop1234567890AB&expires_in=0"
        )

        assert "vk1.a.qwertyuiop1234567890AB" in paths.env_file().read_text(encoding="utf-8")
        assert "принят" in message.answered[0]

    def test_the_message_with_the_key_is_deleted(self, env):
        """Ключ даёт доступ к сообществу — в переписке ему не место."""
        message = env["accept"](
            "https://oauth.vk.ru/blank.html#access_token=vk1.a.qwertyuiop1234567890AB"
        )

        assert message.deleted is True

    def test_the_alert_is_cleared_so_it_can_sound_tomorrow(self, env):
        notifier = StubNotifier()
        alerts.raise_once(
            env["conn"], notifier, chat_id=OWNER, name="vk_token", scope="demo", text="истёк"
        )
        assert alerts.is_raised(env["conn"], "vk_token", "demo")

        env["accept"](
            "https://oauth.vk.ru/blank.html#access_token=vk1.a.qwertyuiop1234567890AB"
        )

        assert not alerts.is_raised(env["conn"], "vk_token", "demo")

    def test_a_stranger_cannot_replace_the_key(self, env):
        """Иначе посторонний подменяет ключ доступа к сообществу."""
        from factory.core import paths

        message = env["accept"](
            "https://oauth.vk.ru/blank.html#access_token=vk1.a.qwertyuiop1234567890AB",
            user=999,
        )

        assert "старый" in paths.env_file().read_text(encoding="utf-8")
        assert "не для вас" in message.answered[0]

    def test_a_stranger_message_is_not_deleted(self, env):
        """Удалять чужие сообщения бот не должен — он их и не принимал."""
        message = env["accept"](
            "https://oauth.vk.ru/blank.html#access_token=vk1.a.qwertyuiop1234567890AB",
            user=999,
        )

        assert message.deleted is False

    def test_garbage_is_explained_not_swallowed(self, env):
        from factory.core import paths

        message = env["accept"]("держи access_token=непонятно_что")

        assert "старый" in paths.env_file().read_text(encoding="utf-8")
        assert "адрес" in message.answered[0].lower()


def _accept(review_bot, conn, projects, text, user_id):
    message = FakeMessage(text=text, from_user=FakeUser(user_id))
    asyncio.run(review_bot._accept_vk_token(conn, projects, message))
    return message
