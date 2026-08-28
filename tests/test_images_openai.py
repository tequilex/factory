"""Провайдер картинок — на моках.

Проверяется то, что выяснилось живыми вызовами RouterAI до написания кода:
как устроен запрос, откуда берётся цена, и что делать с ответами, которые
формально успешны, а по сути нет.
"""

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from factory.core.errors import ProviderError
from factory.core.retry import _cost_of, _is_retryable
from factory.providers.images.openai_compatible import OpenAICompatibleImages

BASE = "https://routerai.ru/api/v1"
MODEL = "black-forest-labs/flux.2-klein-4b"


def png(width: int = 1080, height: int = 1350, colour=(20, 90, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def reply(data: bytes | None = None, *, cost: float | None = 1.56) -> httpx.Response:
    usage = {"cost": cost} if cost is not None else {}
    return httpx.Response(
        200,
        json={
            "data": [{"b64_json": base64.b64encode(data if data is not None else png()).decode()}],
            "usage": usage,
        },
    )


class Recorder:
    def __init__(self, response=None):
        self.requests: list[httpx.Request] = []
        self.response = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if callable(self.response):
            return self.response(request)
        return self.response if self.response is not None else reply()

    @property
    def payload(self) -> dict:
        return json.loads(self.requests[0].content)


def provider(recorder, monkeypatch, **kwargs) -> OpenAICompatibleImages:
    transport = httpx.MockTransport(recorder.handler)
    monkeypatch.setattr(
        "factory.core.http.client_for",
        lambda *a, **kw: httpx.Client(transport=transport, headers=kw.get("headers") or {}),
    )
    kwargs.setdefault("base_url", BASE)
    kwargs.setdefault("api_key", "sk-test-key")
    kwargs.setdefault("model", MODEL)
    return OpenAICompatibleImages(**kwargs)


class TestRequest:
    def test_goes_to_the_configured_address(self, monkeypatch):
        recorder = Recorder()

        provider(recorder, monkeypatch).generate("сцена")

        assert str(recorder.requests[0].url) == f"{BASE}/images/generations"

    def test_key_travels_in_the_authorization_header(self, monkeypatch):
        recorder = Recorder()

        provider(recorder, monkeypatch).generate("сцена")

        assert recorder.requests[0].headers["authorization"] == "Bearer sk-test-key"

    def test_model_prompt_and_single_image_are_sent(self, monkeypatch):
        recorder = Recorder()

        provider(recorder, monkeypatch).generate("woman next to a car")

        payload = recorder.payload
        assert payload["model"] == MODEL
        assert payload["prompt"] == "woman next to a car"
        assert payload["n"] == 1

    def test_requested_size_is_sent(self, monkeypatch):
        recorder = Recorder(reply(png(800, 600)))

        provider(recorder, monkeypatch).generate("сцена", width=800, height=600)

        assert recorder.payload["size"] == "800x600"

    def test_seed_is_sent_when_given(self, monkeypatch):
        recorder = Recorder()

        provider(recorder, monkeypatch).generate("сцена", seed=12345)

        assert recorder.payload["seed"] == 12345

    def test_no_seed_field_when_not_given(self, monkeypatch):
        recorder = Recorder()

        provider(recorder, monkeypatch).generate("сцена")

        assert "seed" not in recorder.payload


class TestReference:
    """Образец персонажа — единственный механизм узнаваемости на этом этапе."""

    def test_reference_travels_as_data_url(self, monkeypatch):
        recorder = Recorder()
        sample = png(64, 64)

        provider(
            recorder, monkeypatch, reference=sample, supports_reference=True
        ).generate("сцена")

        sent = recorder.payload["image"]
        assert sent.startswith("data:image/png;base64,")
        assert base64.b64decode(sent.split(",", 1)[1]) == sample

    def test_no_image_field_without_reference(self, monkeypatch):
        recorder = Recorder()

        provider(recorder, monkeypatch).generate("сцена")

        assert "image" not in recorder.payload

    def test_reference_is_not_sent_to_a_model_that_ignores_it(self, monkeypatch):
        """Проверено живьём: gemini отвечает 200 и рисует другого человека.

        Отправлять образец модели, которая его выбрасывает, — значит гонять
        мегабайт по сети за иллюзию узнаваемости. Конфиг этого не допустит, но
        провайдер обязан держаться и сам: собрать его напрямую может тест или
        будущий вызывающий код.
        """
        recorder = Recorder()

        provider(
            recorder, monkeypatch, reference=png(64, 64), supports_reference=False
        ).generate("сцена")

        assert "image" not in recorder.payload


class TestAnswer:
    def test_picture_is_decoded_from_b64_json(self, monkeypatch):
        original = png()
        recorder = Recorder(reply(original))

        result = provider(recorder, monkeypatch).generate("сцена")

        assert result == original

    def test_cost_comes_from_usage(self, monkeypatch):
        recorder = Recorder(reply(cost=4.33))

        result = provider(recorder, monkeypatch).generate("сцена")

        assert _cost_of(result) == pytest.approx(4.33)

    def test_config_price_used_when_provider_stays_silent(self, monkeypatch):
        recorder = Recorder(reply(cost=None))

        result = provider(recorder, monkeypatch, price_per_image=1.56).generate("сцена")

        assert _cost_of(result) == pytest.approx(1.56)

    def test_reported_cost_wins_over_the_config_price(self, monkeypatch):
        """Фактическая цена важнее расчётной: у реселлеров она плавает."""
        recorder = Recorder(reply(cost=3.72))

        result = provider(recorder, monkeypatch, price_per_image=1.56).generate("сцена")

        assert _cost_of(result) == pytest.approx(3.72)

    def test_unknown_price_stays_unknown(self, monkeypatch):
        recorder = Recorder(reply(cost=None))

        result = provider(recorder, monkeypatch).generate("сцена")

        assert _cost_of(result) is None


class TestSize:
    def test_matching_size_passes_through_untouched(self, monkeypatch):
        original = png(1080, 1350)
        recorder = Recorder(reply(original))

        result = provider(recorder, monkeypatch).generate("сцена")

        assert result == original

    def test_wrong_size_is_brought_to_the_requested_one(self, monkeypatch):
        recorder = Recorder(reply(png(1024, 1024)))

        result = provider(recorder, monkeypatch).generate("сцена")

        assert Image.open(io.BytesIO(result)).size == (1080, 1350)

    def test_wrong_aspect_is_cropped_not_squeezed(self, monkeypatch):
        """Растянуть квадрат под 1080×1350 — значит вытянуть человека на картинке.

        Проверяется по содержимому: широкая картинка из красной левой половины и
        синей правой после приведения к узкому кадру обязана потерять края, а не
        сжаться. Если бы её сжали, красное осталось бы у самого левого пикселя.
        """
        wide = Image.new("RGB", (2000, 1000), (0, 0, 255))
        wide.paste(Image.new("RGB", (400, 1000), (255, 0, 0)), (0, 0))
        buffer = io.BytesIO()
        wide.save(buffer, format="PNG")
        recorder = Recorder(reply(buffer.getvalue()))

        result = provider(recorder, monkeypatch).generate("сцена", width=1000, height=1000)

        image = Image.open(io.BytesIO(result))
        assert image.size == (1000, 1000)
        assert image.getpixel((2, 500))[2] > 200  # левый край стал синим: красное срезано


class TestFailures:
    def test_missing_data_is_a_human_error_not_a_keyerror(self, monkeypatch):
        recorder = Recorder(httpx.Response(200, json={"created": 1}))

        with pytest.raises(ProviderError) as exc:
            provider(recorder, monkeypatch).generate("сцена")

        assert "нет картинки" in str(exc.value)
        assert "image.model" in str(exc.value)

    def test_broken_base64_is_explained(self, monkeypatch):
        recorder = Recorder(httpx.Response(200, json={"data": [{"b64_json": "не base64!"}]}))

        with pytest.raises(ProviderError) as exc:
            provider(recorder, monkeypatch).generate("сцена")

        assert "не раскодировалась" in str(exc.value)

    def test_not_a_picture_is_explained(self, monkeypatch):
        recorder = Recorder(
            httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(b"not a png at all").decode()}]},
            )
        )

        with pytest.raises(ProviderError) as exc:
            provider(recorder, monkeypatch).generate("сцена")

        assert "не открывается как картинка" in str(exc.value)

    def test_missing_model_names_the_config_field(self, monkeypatch):
        recorder = Recorder(httpx.Response(404, text="Model not found"))

        with pytest.raises(ProviderError) as exc:
            provider(recorder, monkeypatch).generate("сцена")

        assert "image.model" in str(exc.value)

    def test_server_error_is_retryable(self, monkeypatch):
        recorder = Recorder(httpx.Response(503, text="upstream"))

        with pytest.raises(ProviderError) as exc:
            provider(recorder, monkeypatch).generate("сцена")

        assert _is_retryable(exc.value)


class TestSpendingLimit:
    """Исчерпанный лимит ключа приходит обычным 429 — и это ловушка.

    По коду он неотличим от «слишком часто стучитесь», но повтором не лечится:
    потолок снимает человек в личном кабинете. Ретраить такое значит сжигать
    попытки поста на событие, которое само не наступит.
    """

    RESPONSE = httpx.Response(
        429, text="API key monthly spending limit exceeded: 61,94 руб. / 60,00 руб."
    )

    def test_it_waits_for_a_human_instead_of_retrying(self, monkeypatch):
        recorder = Recorder(self.RESPONSE)

        with pytest.raises(ProviderError) as exc:
            provider(recorder, monkeypatch).generate("сцена")

        assert exc.value.needs_human is True
        assert not _is_retryable(exc.value)

    def test_it_says_where_the_limit_is_raised(self, monkeypatch):
        recorder = Recorder(self.RESPONSE)

        with pytest.raises(ProviderError) as exc:
            provider(recorder, monkeypatch).generate("сцена")

        assert "личном кабинете" in str(exc.value)
        assert BASE in str(exc.value)

    def test_ordinary_rate_limit_is_still_retried(self, monkeypatch):
        """Обычный 429 обязан остаться повторяемым, иначе лечится одно и ломается другое."""
        recorder = Recorder(httpx.Response(429, text="Too many requests"))

        with pytest.raises(ProviderError) as exc:
            provider(recorder, monkeypatch).generate("сцена")

        assert getattr(exc.value, "needs_human", False) is False
        assert _is_retryable(exc.value)


class TestLora:
    def test_lora_is_refused_loudly(self, monkeypatch):
        """Реселлер раздаёт общие веса; тихо проигнорировать LoRA — худший исход.

        Владелец платил бы за дообученную модель и получал чужого человека, не
        понимая почему.
        """
        recorder = Recorder()

        with pytest.raises(ProviderError) as exc:
            provider(recorder, monkeypatch).generate("сцена", lora="kristina_v1")

        assert "image.lora" in str(exc.value)
        assert recorder.requests == []
