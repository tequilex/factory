"""Тест на сам предохранитель.

Без него фикстура no_network может тихо перестать работать при обновлении httpx,
и тесты начнут ходить в интернет, а заметят это по счёту за API.
"""

import httpx
import pytest


def test_plain_request_is_blocked():
    with pytest.raises(RuntimeError) as excinfo:
        httpx.get("https://example.com/api")

    message = str(excinfo.value)
    assert "Тест попытался сходить в сеть" in message
    assert "GET" in message
    assert "https://example.com/api" in message


def test_client_created_by_hand_is_blocked():
    with pytest.raises(RuntimeError, match="Тест попытался сходить в сеть"):
        with httpx.Client() as client:
            client.post("https://api.openai.com/v1/chat/completions", json={})


def test_client_with_explicit_transport_is_blocked():
    with pytest.raises(RuntimeError, match="Тест попытался сходить в сеть"):
        with httpx.Client(transport=httpx.HTTPTransport()) as client:
            client.get("https://api.vk.com/method/wall.post")


@pytest.mark.anyio
async def test_async_client_is_blocked():
    with pytest.raises(RuntimeError, match="Тест попытался сходить в сеть"):
        async with httpx.AsyncClient() as client:
            await client.get("https://example.com")


def test_mock_transport_still_works():
    """Предохранитель не должен мешать нормальному мокингу."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": 1}))
    with httpx.Client(transport=transport) as client:
        response = client.get("https://example.com")

    assert response.json() == {"ok": 1}


@pytest.fixture
def anyio_backend():
    return "asyncio"
