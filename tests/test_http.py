"""Фабрика HTTP-клиентов.

Ключевое требование — раздельный прокси на провайдера. С российского IP плохо
ходить в OpenAI, с зарубежного — в VK; один глобальный прокси эту задачу не
решает.
"""

import httpx
import pytest

from factory.core import http

PROXY_ENV_VARS = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "LLM_PROXY",
    "IMAGE_PROXY",
    "VK_PROXY",
    "CUSTOM_PROXY",
]


@pytest.fixture(autouse=True)
def clean_proxies(monkeypatch):
    for name in PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def proxy_of(client: httpx.Client) -> str | None:
    """Достаёт адрес прокси из собранного клиента.

    Публичного способа спросить у httpx.Client про его прокси нет, поэтому здесь
    приходится лезть во внутренности. Основная гарантия — тесты proxy_for()
    выше: они проверяют саму логику выбора и от версии httpx не зависят. Этот
    хелпер проверяет только, что выбранное значение доехало до клиента.

    В httpx 0.28 прокси хранится не в транспорте клиента, а в mounts: аргумент
    proxy разворачивается в правило «all://» с отдельным транспортом.
    """
    for transport in client._mounts.values():
        pool = getattr(transport, "_pool", None)
        url = getattr(pool, "_proxy_url", None)
        if url is not None:
            return f"{url.scheme.decode()}://{url.host.decode()}:{url.port}"
    return None


class TestProxySelection:
    def test_no_proxy_configured_means_direct(self):
        assert http.proxy_for("llm") is None

    def test_provider_specific_variable_is_used(self, monkeypatch):
        monkeypatch.setenv("LLM_PROXY", "http://llm-proxy:8080")

        assert http.proxy_for("llm") == "http://llm-proxy:8080"

    def test_provider_variable_wins_over_global(self, monkeypatch):
        monkeypatch.setenv("LLM_PROXY", "http://llm-proxy:8080")
        monkeypatch.setenv("HTTPS_PROXY", "http://global:3128")

        assert http.proxy_for("llm") == "http://llm-proxy:8080"

    def test_global_is_the_fallback(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://global:3128")

        assert http.proxy_for("llm") == "http://global:3128"

    def test_config_named_variable_wins_over_everything(self, monkeypatch):
        """В конфиге можно указать своё имя переменной — оно главнее конвенции."""
        monkeypatch.setenv("CUSTOM_PROXY", "http://custom:8080")
        monkeypatch.setenv("LLM_PROXY", "http://by-convention:8080")
        monkeypatch.setenv("HTTPS_PROXY", "http://global:3128")

        assert http.proxy_for("llm", proxy_env="CUSTOM_PROXY") == "http://custom:8080"

    def test_missing_config_variable_falls_through(self, monkeypatch):
        """Имя указано, но переменной нет — не падаем, идём дальше по цепочке."""
        monkeypatch.setenv("HTTPS_PROXY", "http://global:3128")

        assert http.proxy_for("llm", proxy_env="НЕТ_ТАКОЙ") == "http://global:3128"

    def test_providers_are_proxied_independently(self, monkeypatch):
        """Главный сценарий: в LLM через прокси, в VK — напрямую."""
        monkeypatch.setenv("LLM_PROXY", "http://abroad:8080")
        monkeypatch.setenv("IMAGE_PROXY", "http://abroad:8080")

        assert http.proxy_for("llm") == "http://abroad:8080"
        assert http.proxy_for("image") == "http://abroad:8080"
        assert http.proxy_for("vk") is None

    def test_lowercase_global_variable_is_honoured(self, monkeypatch):
        monkeypatch.setenv("https_proxy", "http://global:3128")

        assert http.proxy_for("vk") == "http://global:3128"

    def test_empty_value_is_treated_as_unset(self, monkeypatch):
        """Пустая строка в .env — частая опечатка, она не должна ломать запросы."""
        monkeypatch.setenv("LLM_PROXY", "")
        monkeypatch.setenv("HTTPS_PROXY", "http://global:3128")

        assert http.proxy_for("llm") == "http://global:3128"


class TestClient:
    def test_timeouts_match_the_spec(self):
        with http.client_for("llm") as client:
            assert client.timeout.connect == 10.0
            assert client.timeout.read == 120.0

    def test_base_url_is_configurable(self):
        """Пользователь может ходить через реселлера, а не в сам вендорский адрес."""
        with http.client_for("llm", base_url="https://reseller.example/v1") as client:
            assert str(client.base_url).rstrip("/") == "https://reseller.example/v1"

    def test_headers_are_merged_with_defaults(self):
        with http.client_for("llm", headers={"Authorization": "Bearer x"}) as client:
            assert client.headers["authorization"] == "Bearer x"
            assert "factory" in client.headers["user-agent"]

    def test_proxy_is_applied_to_the_client(self, monkeypatch):
        monkeypatch.setenv("LLM_PROXY", "http://llm-proxy:8080")

        with http.client_for("llm") as client:
            assert proxy_of(client) == "http://llm-proxy:8080"

    def test_no_proxy_means_no_proxy_on_the_client(self):
        with http.client_for("vk") as client:
            assert proxy_of(client) is None

    def test_redirects_are_followed(self):
        """VK отдаёт upload_url отдельным редиректом — без этого загрузка сломается."""
        with http.client_for("vk") as client:
            assert client.follow_redirects is True


def test_no_module_creates_httpx_clients_directly():
    """Прямой httpx.Client() теряет прокси, таймауты и затирание секретов."""
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent / "factory"
    offenders = []

    for path in package.rglob("*.py"):
        if path.name == "http.py":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "httpx.Client(" in stripped or "httpx.AsyncClient(" in stripped:
                offenders.append(f"{path.relative_to(package.parent)}:{lineno}")

    assert not offenders, (
        "клиенты httpx создаются в обход core/http.py: " + ", ".join(offenders)
    )


class TestSocksProxies:
    """SOCKS нужен, потому что дешёвые прокси почти всегда именно такие.

    Без пакета socksio httpx падает на создании клиента с socks5://, и падает
    не при настройке, а в бою — на первом же запросе к Telegram.
    """

    def test_a_socks_proxy_can_be_used(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_PROXY", "socks5://127.0.0.1:1080")

        with http.client_for("telegram") as client:
            assert client is not None

    def test_an_http_proxy_still_works(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_PROXY", "http://127.0.0.1:3128")

        with http.client_for("telegram") as client:
            assert client is not None

    def test_each_provider_keeps_its_own_route(self, monkeypatch):
        """Главное свойство: ВК идёт напрямую, когда Telegram идёт через прокси."""
        monkeypatch.setenv("TELEGRAM_PROXY", "socks5://127.0.0.1:1080")
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("VK_PROXY", raising=False)

        assert http.proxy_for("telegram") == "socks5://127.0.0.1:1080"
        assert http.proxy_for("vk") is None
