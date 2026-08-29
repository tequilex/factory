"""Вход в панель.

Панель управляет публикациями в живые сообщества. Tailscale отсекает чужих на
уровне сети, но пароль закрывает то, чего сеть не видит: потерянный телефон и
чужие руки на разблокированном экране.

Проверяется здесь не «форма отправляется», а то, чем вход держится: подпись,
срок, отсутствие данных без входа и отсутствие пароля в логах.
"""

import time

import pytest
from fastapi.testclient import TestClient

from factory.core.errors import ConfigError
from factory.panel import auth
from factory.panel.app import create_app

PASSWORD = "правильный-пароль-42"


@pytest.fixture
def panel(tmp_env, monkeypatch):
    """Панель с заданным паролем. Секреты — во временном файле, как и база."""
    monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
    monkeypatch.delenv(auth.SECRET_ENV, raising=False)
    auth.set_password(PASSWORD)
    return TestClient(create_app())


class TestPassword:
    def test_the_right_password_is_accepted(self, panel):
        assert auth.check_password(PASSWORD) is True

    def test_a_wrong_password_is_refused(self, panel):
        assert auth.check_password("другой") is False

    def test_the_hash_does_not_contain_the_password(self, panel):
        """Файл секретов читают глазами — пароль оттуда восстанавливаться не должен."""
        import os

        stored = os.environ[auth.PASSWORD_ENV]

        assert PASSWORD not in stored
        assert stored.startswith("scrypt$")

    def test_two_hashes_of_one_password_differ(self, panel):
        """Соль на месте: одинаковые хеши выдали бы одинаковые пароли у разных групп."""
        assert auth.hash_password(PASSWORD) != auth.hash_password(PASSWORD)

    def test_an_empty_password_is_refused(self, tmp_env):
        with pytest.raises(ConfigError):
            auth.hash_password("")

    def test_a_broken_stored_hash_is_not_a_crash(self, panel):
        """Испорченный файл секретов — отказ во входе, а не трейсбек на экране."""
        assert auth.verify_password(PASSWORD, "мусор") is False
        assert auth.verify_password(PASSWORD, "scrypt$нет$полей") is False


class TestCookie:
    def test_a_fresh_cookie_is_valid(self, panel):
        assert auth.check_cookie(auth.issue_cookie()) is True

    def test_a_forged_signature_is_refused(self, panel):
        """Главная проверка входа: без секрета куку не подделать."""
        good = auth.issue_cookie()
        payload, signature = good.rsplit(".", 1)
        forged = f"{payload}.{'0' * len(signature)}"

        assert auth.check_cookie(forged) is False

    def test_a_changed_expiry_is_refused(self, panel):
        """Срок входит в подпись, иначе его можно было бы просто дописать."""
        good = auth.issue_cookie()
        version, expires, signature = good.split(".")
        tampered = f"{version}.{int(expires) + 10 ** 6}.{signature}"

        assert auth.check_cookie(tampered) is False

    def test_an_expired_cookie_is_refused(self, panel):
        issued = auth.issue_cookie(now=time.time() - auth.SHORT_HOURS * 3600 - 60)

        assert auth.check_cookie(issued) is False

    def test_trusting_a_device_lasts_longer(self, panel):
        """«Не спрашивать 30 дней» должно правда означать тридцать дней."""
        moment = time.time()
        short = int(auth.issue_cookie(now=moment).split(".")[1])
        long = int(auth.issue_cookie(trusted=True, now=moment).split(".")[1])

        assert long - short == pytest.approx(
            auth.DEFAULT_DAYS * 86400 - auth.SHORT_HOURS * 3600, abs=2
        )

    def test_garbage_is_refused(self, panel):
        for value in ("", None, "мусор", "v1.нечисло.подпись", "v2.1.2"):
            assert auth.check_cookie(value) is False

    def test_changing_the_secret_invalidates_everything(self, panel):
        """«Выйти на всех устройствах» — это смена секрета, сессий-то нет."""
        issued = auth.issue_cookie(trusted=True)
        assert auth.check_cookie(issued) is True

        auth.reset_secret()

        assert auth.check_cookie(issued) is False


class TestHttp:
    def test_data_needs_a_login(self, panel):
        assert panel.get("/api/session").status_code == 401

    def test_login_opens_the_door(self, panel):
        assert panel.post("/api/login", json={"password": PASSWORD}).status_code == 200
        assert panel.get("/api/session").status_code == 200

    def test_a_wrong_password_gives_no_cookie(self, panel):
        response = panel.post("/api/login", json={"password": "не тот"})

        assert response.status_code == 401
        assert auth.COOKIE_NAME not in response.cookies
        assert panel.get("/api/session").status_code == 401

    def test_the_cookie_is_hidden_from_scripts(self, panel):
        """HttpOnly: даже если на страницу затечёт чужой скрипт, куку он не прочитает."""
        response = panel.post("/api/login", json={"password": PASSWORD})

        assert "httponly" in response.headers["set-cookie"].lower()

    def test_logout_closes_the_door(self, panel):
        panel.post("/api/login", json={"password": PASSWORD})

        panel.post("/api/logout")

        assert panel.get("/api/session").status_code == 401

    def test_no_password_set_is_a_setting_not_a_refusal(self, tmp_env, monkeypatch):
        """503 с инструкцией, а не 401: владельцу нужно понять, что делать."""
        monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
        client = TestClient(create_app())

        response = client.post("/api/login", json={"password": "хоть какой"})

        assert response.status_code == 503
        assert "panel-password" in response.json()["detail"]

    def test_the_api_map_is_not_public(self, panel):
        """Список ручек без входа — подсказка тому, кто оказался в сети."""
        assert panel.get("/openapi.json").status_code == 404
        assert panel.get("/docs").status_code == 404


class TestSecrecy:
    def test_the_password_never_reaches_the_log(self, panel, caplog):
        """Пароль не должен попасть в лог ни при удаче, ни при промахе."""
        with caplog.at_level("DEBUG"):
            panel.post("/api/login", json={"password": PASSWORD})
            panel.post("/api/login", json={"password": "не тот"})

        assert PASSWORD not in caplog.text
        assert "не тот" not in caplog.text
