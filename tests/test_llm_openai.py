"""Провайдер текстовой модели — на моках.

Здесь проверяется то, что выяснилось живыми вызовами до написания кода: как
устроен запрос, и что происходит при каждом из способов, которыми этот API
умеет отвечать неправильно.
"""

import json

import httpx
import pytest

from factory.core.errors import ProviderError
from factory.core.retry import (
    MAX_RETRY_AFTER_SEC,
    _cost_of,
    _is_retryable,
    _retry_after_sec,
    tracked_call,
)
from factory.providers.base import PostDraft
from factory.providers.llm.openai_compatible import OpenAICompatibleLLM

BASE = "https://routerai.ru/api/v1"

GOOD_POST = {
    "title": "Как выбрать зимние шины",
    "body": "Текст поста подходящей длины.",
    "question": "А вы уже переобулись?",
}


class Recorder:
    """Ловит запрос и отвечает по сценарию."""

    def __init__(self, response=None):
        self.requests: list[httpx.Request] = []
        self.response = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if callable(self.response):
            return self.response(request)
        return self.response or reply(json.dumps(GOOD_POST, ensure_ascii=False))

    @property
    def payload(self) -> dict:
        return json.loads(self.requests[0].content)


def reply(content: str, *, prompt=100, completion=200, extra=None) -> httpx.Response:
    message = {"role": "assistant", "content": content}
    if extra:
        message.update(extra)
    return httpx.Response(
        200,
        json={
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        },
    )


def provider_with_key(recorder, monkeypatch, key: str):
    transport = httpx.MockTransport(recorder.handler)
    monkeypatch.setattr(
        "factory.core.http.client_for",
        lambda *a, **kw: httpx.Client(transport=transport, headers=kw.get("headers") or {}),
    )
    return OpenAICompatibleLLM(
        base_url=BASE, api_key=key, model="deepseek/deepseek-v3.2"
    )


def provider(recorder, monkeypatch, **kwargs):
    transport = httpx.MockTransport(recorder.handler)
    monkeypatch.setattr(
        "factory.core.http.client_for",
        lambda *a, **kw: httpx.Client(transport=transport, headers=kw.get("headers") or {}),
    )
    return OpenAICompatibleLLM(
        base_url=BASE,
        api_key="sk-test-key",
        model="deepseek/deepseek-v3.2",
        **kwargs,
    )


class TestRequest:
    def test_goes_to_the_configured_address(self, monkeypatch):
        recorder = Recorder()

        provider(recorder, monkeypatch).complete("система", "запрос")

        assert str(recorder.requests[0].url) == f"{BASE}/chat/completions"

    def test_key_travels_in_the_authorization_header(self, monkeypatch):
        recorder = Recorder()

        provider(recorder, monkeypatch).complete("система", "запрос")

        assert recorder.requests[0].headers["authorization"] == "Bearer sk-test-key"

    def test_both_messages_are_sent(self, monkeypatch):
        recorder = Recorder()

        provider(recorder, monkeypatch).complete("роль персонажа", "тема поста")

        messages = recorder.payload["messages"]
        assert messages[0] == {"role": "system", "content": "роль персонажа"}
        assert messages[1] == {"role": "user", "content": "тема поста"}

    def test_model_and_limits_come_from_the_config(self, monkeypatch):
        recorder = Recorder()

        provider(recorder, monkeypatch, max_tokens=7777, temperature=0.3).complete("s", "u")

        payload = recorder.payload
        assert payload["model"] == "deepseek/deepseek-v3.2"
        assert payload["max_tokens"] == 7777
        assert payload["temperature"] == 0.3

    def test_default_token_limit_leaves_room_for_reasoning(self, monkeypatch):
        """Модель с рассуждениями при тесном лимите возвращает пустоту.

        Проверено живьём: при 1200 токенах ответ пустой, при 4000 — нормальный.
        Число литералом: оно выбрано по замеру, а не выведено из формулы.
        """
        recorder = Recorder()

        provider(recorder, monkeypatch).complete("s", "u")

        assert recorder.payload["max_tokens"] == 4000


class TestStructuredOutput:
    def test_schema_becomes_a_strict_response_format(self, monkeypatch):
        recorder = Recorder()

        provider(recorder, monkeypatch).complete("s", "u", schema=PostDraft)

        fmt = recorder.payload["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["strict"] is True
        assert set(fmt["json_schema"]["schema"]["properties"]) == {"title", "body", "question"}

    def test_strict_mode_requires_every_field(self, monkeypatch):
        """Строгий режим отвергнет схему, где required не покрывает все поля."""
        recorder = Recorder()

        provider(recorder, monkeypatch).complete("s", "u", schema=PostDraft)

        schema = recorder.payload["response_format"]["json_schema"]["schema"]
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["additionalProperties"] is False

    def test_answer_becomes_an_object(self, monkeypatch):
        recorder = Recorder()

        result = provider(recorder, monkeypatch).complete("s", "u", schema=PostDraft)

        assert isinstance(result, PostDraft)
        assert result.title == "Как выбрать зимние шины"

    def test_without_a_schema_no_response_format_is_sent(self, monkeypatch):
        recorder = Recorder(reply("просто текст"))

        result = provider(recorder, monkeypatch).complete("s", "u")

        assert "response_format" not in recorder.payload
        assert result == "просто текст"

    def test_json_wrapped_in_markdown_is_still_parsed(self, monkeypatch):
        """Модели дописывают ```json даже когда их просят не делать этого."""
        fenced = "```json\n" + json.dumps(GOOD_POST, ensure_ascii=False) + "\n```"
        recorder = Recorder(reply(fenced))

        result = provider(recorder, monkeypatch).complete("s", "u", schema=PostDraft)

        assert result.title == "Как выбрать зимние шины"

    def test_json_with_chatter_around_it_is_still_parsed(self, monkeypatch):
        noisy = "Вот пост:\n" + json.dumps(GOOD_POST, ensure_ascii=False) + "\nГотово!"
        recorder = Recorder(reply(noisy))

        result = provider(recorder, monkeypatch).complete("s", "u", schema=PostDraft)

        assert result.question == "А вы уже переобулись?"


class TestEmptyAnswer:
    """Главная находка живых вызовов: пустой ответ приходит с кодом 200."""

    def test_empty_content_is_a_failure_not_a_success(self, monkeypatch):
        recorder = Recorder(reply(""))

        with pytest.raises(ProviderError):
            provider(recorder, monkeypatch).complete("s", "u", schema=PostDraft)

    def test_the_message_names_the_actual_fix(self, monkeypatch):
        """«Ошибка разбора JSON» владельцу бесполезна, «увеличь лимит» — нет."""
        recorder = Recorder(reply("", completion=1200))

        with pytest.raises(ProviderError) as excinfo:
            provider(recorder, monkeypatch, max_tokens=1200).complete("s", "u", schema=PostDraft)

        message = str(excinfo.value)
        assert "llm.max_tokens" in message
        assert "1200" in message
        assert "рассужден" in message

    def test_whitespace_only_counts_as_empty(self, monkeypatch):
        recorder = Recorder(reply("   \n  "))

        with pytest.raises(ProviderError, match="пустой ответ"):
            provider(recorder, monkeypatch).complete("s", "u")

    def test_reasoning_length_is_reported(self, monkeypatch):
        recorder = Recorder(reply("", extra={"reasoning": "я долго думал" * 50}))

        with pytest.raises(ProviderError) as excinfo:
            provider(recorder, monkeypatch).complete("s", "u")

        assert "650 символов" in str(excinfo.value)


class TestBadAnswers:
    def test_not_json_at_all_is_explained(self, monkeypatch):
        recorder = Recorder(reply("Заголовок: Как выбрать шины\n\nТекст поста…"))

        with pytest.raises(ProviderError) as excinfo:
            provider(recorder, monkeypatch).complete("s", "u", schema=PostDraft)

        message = str(excinfo.value)
        assert "не по заданной структуре" in message
        assert "llm.model" in message

    def test_missing_field_is_explained_by_name(self, monkeypatch):
        recorder = Recorder(reply(json.dumps({"title": "Заголовок"})))

        with pytest.raises(ProviderError) as excinfo:
            provider(recorder, monkeypatch).complete("s", "u", schema=PostDraft)

        message = str(excinfo.value)
        assert "body" in message
        assert "question" in message

    def test_too_long_title_is_reported(self, monkeypatch):
        """Схема требует не больше 60 символов — это ограничение обложки."""
        bad = {**GOOD_POST, "title": "а" * 80}
        recorder = Recorder(reply(json.dumps(bad, ensure_ascii=False)))

        with pytest.raises(ProviderError) as excinfo:
            provider(recorder, monkeypatch).complete("s", "u", schema=PostDraft)

        assert "title" in str(excinfo.value)

    def test_answer_without_choices_is_explained(self, monkeypatch):
        recorder = Recorder(httpx.Response(200, json={"id": "x"}))

        with pytest.raises(ProviderError) as excinfo:
            provider(recorder, monkeypatch).complete("s", "u")

        assert "llm.model" in str(excinfo.value)

    def test_non_json_response_body_is_explained(self, monkeypatch):
        recorder = Recorder(httpx.Response(200, text="<html>сбой</html>"))

        with pytest.raises(ProviderError, match="не-JSON"):
            provider(recorder, monkeypatch).complete("s", "u")


class TestHttpErrors:
    @pytest.mark.parametrize(
        "status,marker",
        [
            (401, "LLM_API_KEY"),
            (402, "Пополни счёт"),
            (404, "llm.model"),
            (429, "повторит позже"),
            (500, "провайдера"),
        ],
    )
    def test_each_code_gets_its_own_advice(self, monkeypatch, status, marker):
        recorder = Recorder(httpx.Response(status, json={"error": "beda"}))

        with pytest.raises(ProviderError) as excinfo:
            provider(recorder, monkeypatch).complete("s", "u")

        assert marker in str(excinfo.value)


class TestCost:
    def test_cost_is_computed_from_usage(self, monkeypatch):
        recorder = Recorder(reply(json.dumps(GOOD_POST, ensure_ascii=False),
                                  prompt=1_000_000, completion=1_000_000))

        result = provider(
            recorder, monkeypatch, price_input_per_1m=22.5, price_output_per_1m=33.4
        ).complete("s", "u", schema=PostDraft)

        assert _cost_of(result) == pytest.approx(55.9)

    def test_small_calls_are_priced_proportionally(self, monkeypatch):
        recorder = Recorder(reply(json.dumps(GOOD_POST, ensure_ascii=False),
                                  prompt=288, completion=618))

        result = provider(
            recorder, monkeypatch, price_input_per_1m=22.5, price_output_per_1m=33.4
        ).complete("s", "u", schema=PostDraft)

        expected = (288 * 22.5 + 618 * 33.4) / 1_000_000
        assert _cost_of(result) == pytest.approx(expected)

    def test_without_prices_the_call_still_works(self, monkeypatch):
        """Цены необязательны: владелец может их не знать или не хотеть считать."""
        recorder = Recorder()

        result = provider(recorder, monkeypatch).complete("s", "u", schema=PostDraft)

        assert isinstance(result, PostDraft)
        assert _cost_of(result) is None

    def test_unknown_price_is_not_recorded_as_zero(self, monkeypatch):
        """Ноль вместо неизвестности занизил бы отчёт о тратах."""
        recorder = Recorder()

        result = provider(recorder, monkeypatch, price_input_per_1m=22.5).complete(
            "s", "u", schema=PostDraft
        )

        assert _cost_of(result) is None


class TestKeyValidation:
    """Ключ, скопированный с русского сайта, может содержать кириллические двойники.

    Русская «с» выглядит как латинская «c», но в HTTP-заголовок не проходит.
    Без проверки ошибка вылезает внутри httpx как UnicodeEncodeError — по такому
    сообщению владелец не поймёт ничего.
    """

    def test_cyrillic_lookalike_in_the_key_is_caught_early(self, monkeypatch):
        recorder = Recorder()

        with pytest.raises(ProviderError) as excinfo:
            provider_with_key(recorder, monkeypatch, "sk-teсt")  # «с» русская

        message = str(excinfo.value)
        assert "с" in message
        assert "скопирован" in message.lower() or "Скопируй" in message

    def test_the_offending_character_is_named(self, monkeypatch):
        recorder = Recorder()

        with pytest.raises(ProviderError) as excinfo:
            provider_with_key(recorder, monkeypatch, "sk-абв")

        why = str(excinfo.value)
        for ch in "абв":
            assert ch in why

    def test_a_normal_key_passes(self, monkeypatch):
        recorder = Recorder()

        result = provider_with_key(recorder, monkeypatch, "sk-proj-AbC123").complete("s", "u")

        assert result


class TestTransientFailuresAreRetried:
    """Перегрузка на стороне провайдера — повод повторить, а не сдаться.

    Провайдер переводит ответ сервера в понятную человеку ошибку. Если при этом
    теряется код ответа, механизм повторов видит обычный отказ: пост тратит по
    одной попытке за тик и через пять отказов уходит в failed, хотя всё это
    время достаточно было подождать минуту.
    """

    def test_429_is_marked_retryable(self, monkeypatch):
        recorder = Recorder(httpx.Response(429, text="rate limited"))
        llm = provider(recorder, monkeypatch)

        with pytest.raises(ProviderError) as excinfo:
            llm.complete("s", "u", schema=PostDraft)

        assert _is_retryable(excinfo.value)
        assert excinfo.value.status_code == 429

    def test_server_errors_are_marked_retryable(self, monkeypatch):
        recorder = Recorder(httpx.Response(503, text="upstream down"))
        llm = provider(recorder, monkeypatch)

        with pytest.raises(ProviderError) as excinfo:
            llm.complete("s", "u", schema=PostDraft)

        assert _is_retryable(excinfo.value)

    def test_bad_key_is_not_retried(self, monkeypatch):
        """Повторять неверный ключ бессмысленно: он не исправится сам."""
        recorder = Recorder(httpx.Response(401, text="unauthorized"))
        llm = provider(recorder, monkeypatch)

        with pytest.raises(ProviderError) as excinfo:
            llm.complete("s", "u", schema=PostDraft)

        assert not _is_retryable(excinfo.value)

    def test_retry_after_is_carried_through(self, monkeypatch):
        recorder = Recorder(httpx.Response(429, headers={"Retry-After": "7"}, text=""))
        llm = provider(recorder, monkeypatch)

        with pytest.raises(ProviderError) as excinfo:
            llm.complete("s", "u", schema=PostDraft)

        assert _retry_after_sec(excinfo.value) == 7.0

    def test_absurd_retry_after_is_capped(self, monkeypatch):
        """Просьбу подождать час нельзя выполнять в цикле сна с блокировкой тика."""
        recorder = Recorder(httpx.Response(429, headers={"Retry-After": "3600"}, text=""))
        llm = provider(recorder, monkeypatch)

        with pytest.raises(ProviderError) as excinfo:
            llm.complete("s", "u", schema=PostDraft)

        assert _retry_after_sec(excinfo.value) == MAX_RETRY_AFTER_SEC

    def test_the_step_really_calls_the_model_again(self, monkeypatch):
        """Проверка всей цепочки, а не только флага: попыток должно быть три."""
        attempts = {"n": 0}

        def flaky(request):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(429, headers={"Retry-After": "0"}, text="")
            return reply(json.dumps(GOOD_POST, ensure_ascii=False))

        recorder = Recorder(flaky)
        llm = provider(recorder, monkeypatch)

        @tracked_call("probe")
        def call():
            return llm.complete("s", "u", schema=PostDraft)

        assert call().title == GOOD_POST["title"]
        assert attempts["n"] == 3


class TestPaidFailuresAreStillCounted:
    """Модель берёт деньги и за ответ, который не разобрался."""

    def test_unparseable_answer_carries_its_price(self, monkeypatch):
        recorder = Recorder(reply("вообще не JSON", prompt=1_000_000, completion=0))
        llm = provider(recorder, monkeypatch, price_input_per_1m=10.0, price_output_per_1m=20.0)

        with pytest.raises(ProviderError) as excinfo:
            llm.complete("s", "u", schema=PostDraft)

        assert excinfo.value.cost == 10.0

    def test_answer_missing_fields_carries_its_price(self, monkeypatch):
        recorder = Recorder(reply(json.dumps({"title": "нет остальных полей"}), prompt=1_000_000, completion=0))
        llm = provider(recorder, monkeypatch, price_input_per_1m=10.0, price_output_per_1m=20.0)

        with pytest.raises(ProviderError) as excinfo:
            llm.complete("s", "u", schema=PostDraft)

        assert excinfo.value.cost == 10.0

    def test_empty_answer_carries_its_price(self, monkeypatch):
        """Самый обидный случай: заплачено за размышления, ответа нет."""
        recorder = Recorder(reply("", prompt=0, completion=1_000_000))
        llm = provider(recorder, monkeypatch, price_input_per_1m=10.0, price_output_per_1m=20.0)

        with pytest.raises(ProviderError) as excinfo:
            llm.complete("s", "u", schema=PostDraft)

        assert excinfo.value.cost == 20.0

    def test_without_prices_nothing_is_invented(self, monkeypatch):
        recorder = Recorder(reply("не JSON"))
        llm = provider(recorder, monkeypatch)

        with pytest.raises(ProviderError) as excinfo:
            llm.complete("s", "u", schema=PostDraft)

        assert excinfo.value.cost is None
