"""Any OpenAI-compatible chat API: OpenAI itself, a reseller, a local gateway.

``base_url`` is configurable on purpose — the owner may be going through a
reseller because the vendor's own endpoint is unreachable from where the system
runs. That is the normal case here, not the exception.

Four things about this API were established by calling it for real before this
module was written, and each one shapes the code:

* ``response_format`` with a JSON schema **works** and is the main path;
* ``response_format: json_object`` **does not** — the model answers with ordinary
  prose. So the fallback is an instruction in the prompt plus parsing, not that;
* a reasoning model given a tight token budget spends it all on thinking and
  returns an **empty answer with HTTP 200**. Hence the generous default limit;
* that empty answer is indistinguishable from success until something tries to
  parse it. So it is caught here, with a message naming the actual fix.

Retries are not done here: the calling step is already wrapped in
``tracked_call``, which retries transient failures and records the run.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from factory.core import http
from factory.core.errors import ProviderError
from factory.core.logging import get_logger
from factory.core.retry import with_cost

log = get_logger(__name__)


def _advice(status: int, body: str) -> str:
    if status == 401:
        return (
            "Ключ доступа не принят. Проверь строку LLM_API_KEY в файле секретов: "
            "возможно, скопирован не целиком или отозван."
        )
    if status == 402:
        return (
            "Закончились деньги на балансе провайдера. Пополни счёт — "
            "система продолжит сама, посты ждут в очереди."
        )
    if status == 404:
        return (
            "Адрес или название модели не найдены. Проверь llm.model в конфиге "
            "проекта и LLM_BASE_URL в файле секретов."
        )
    if status == 429:
        return "Слишком много запросов. Система повторит позже сама."
    if 500 <= status < 600:
        return "Сбой на стороне провайдера. Система повторит позже сама."
    return f"Ответ провайдера: {body[:200]}"


def _checked_key(key: str) -> str:
    """Ключ должен быть латиницей: HTTP-заголовки не переносят кириллицу.

    Ловушка для копипаста с русскоязычного сайта: русская «с» неотличима на вид
    от латинской «c», а ошибка вылезет глубоко внутри HTTP-библиотеки в виде
    UnicodeEncodeError — сообщение, по которому владелец ничего не поймёт.
    """
    if key.isascii():
        return key

    culprits = sorted({ch for ch in key if not ch.isascii()})
    raise ProviderError(
        "В ключе доступа есть символы, недопустимые в HTTP-заголовке.",
        why=(
            f"Найдены нелатинские символы: {' '.join(culprits)}. "
            "Обычно это следы копирования: русские буквы «с», «е», «о», «а» "
            "выглядят как латинские, но кодируются иначе."
        ),
        what_to_do=(
            "Скопируй ключ заново из личного кабинета провайдера и вставь в "
            "файл секретов. Проверить можно так: "
            "grep LLM_API_KEY ~/factory-data/.env | LC_ALL=C grep -P '[^\\x00-\\x7F]'"
        ),
    )


def _extract_json(text: str) -> str:
    """Достаёт объект из ответа, что бы модель вокруг него ни дописала.

    Берётся всё между первой открывающей и последней закрывающей скобкой. Этого
    достаточно и для блока ```json, и для пояснений до или после — отдельная
    обработка разметки была бы дублирующей веткой, которую нечем проверить.
    """
    stripped = text.strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


class OpenAICompatibleLLM:
    """Текстовая модель через OpenAI-совместимый API."""

    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        max_tokens: int = 4000,
        temperature: float = 1.0,
        price_input_per_1m: float | None = None,
        price_output_per_1m: float | None = None,
        proxy_env: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = _checked_key(api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.price_input_per_1m = price_input_per_1m
        self.price_output_per_1m = price_output_per_1m
        self.proxy_env = proxy_env
        self.calls = 0

    def _client(self) -> httpx.Client:
        return http.client_for(
            "llm",
            proxy_env=self.proxy_env,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    def _cost(self, usage: dict) -> float | None:
        """Стоимость вызова в валюте провайдера, если цены заданы в конфиге."""
        if self.price_input_per_1m is None or self.price_output_per_1m is None:
            return None
        prompt = int(usage.get("prompt_tokens", 0))
        completion = int(usage.get("completion_tokens", 0))
        return (
            prompt * self.price_input_per_1m + completion * self.price_output_per_1m
        ) / 1_000_000

    def complete(
        self, system: str, user: str, *, schema: type[BaseModel] | None = None
    ) -> str | BaseModel:
        self.calls += 1

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__.lower(),
                    "strict": True,
                    "schema": _json_schema_of(schema),
                },
            }

        with self._client() as client:
            response = client.post(f"{self.base_url}/chat/completions", json=payload)

        if response.status_code != 200:
            raise ProviderError(
                f"Модель не ответила: код {response.status_code}.",
                why=f"Модель {self.model}, адрес {self.base_url}.",
                what_to_do=_advice(response.status_code, response.text),
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                "Провайдер вернул не-JSON вместо ответа модели.",
                why=f"Первые символы: {response.text[:120]!r}",
                what_to_do="Обычно это временный сбой. Система повторит позже.",
            ) from exc

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(
                "В ответе провайдера нет текста модели.",
                why=f"Пришло: {json.dumps(data, ensure_ascii=False)[:200]}",
                what_to_do="Проверь название модели в llm.model конфига проекта.",
            ) from exc

        content = (message.get("content") or "").strip()
        usage = data.get("usage") or {}

        if not content:
            # Модель с рассуждениями тратит весь бюджет на размышления и
            # возвращает пустоту с кодом 200. Проверено живьём на deepseek-v4.
            reasoning = len(message.get("reasoning") or message.get("reasoning_content") or "")
            raise ProviderError(
                "Модель вернула пустой ответ.",
                why=(
                    f"Потрачено токенов на выход: {usage.get('completion_tokens', '?')}, "
                    f"из них на рассуждения {reasoning} символов. "
                    "Модели с рассуждениями тратят лимит на размышления, и на сам "
                    "ответ его не остаётся."
                ),
                what_to_do=(
                    f"Увеличь llm.max_tokens в конфиге проекта — сейчас {self.max_tokens}. "
                    "Либо выбери модель без рассуждений."
                ),
            )

        log.info(
            "ответ модели получен",
            extra={
                "model": self.model,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            },
        )

        if schema is None:
            return with_cost(_Text(content), self._cost(usage))

        return self._parse(content, schema, usage)

    def _parse(self, content: str, schema: type[BaseModel], usage: dict) -> BaseModel:
        raw = _extract_json(content)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "Модель ответила не по заданной структуре.",
                why=f"Ожидался JSON, пришло: {content[:200]!r}",
                what_to_do=(
                    "Обычно помогает повтор — система попробует сама. Если "
                    "повторяется, модель плохо держит формат: смени её в "
                    "llm.model конфига проекта."
                ),
            ) from exc

        try:
            result = schema(**parsed)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            raise ProviderError(
                "Ответ модели не подошёл под нужные поля.",
                why=problems,
                what_to_do=(
                    "Система повторит запрос. Если повторяется — смени модель "
                    "в llm.model конфига проекта."
                ),
            ) from exc

        return with_cost(result, self._cost(usage))


class _Text(str):
    """Строка, к которой можно прикрепить стоимость вызова."""


def _json_schema_of(model: type[BaseModel]) -> dict:
    """Схема pydantic в виде, который принимают строгие структурированные ответы."""
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema["additionalProperties"] = False
    # Строгий режим требует, чтобы в required были перечислены все поля.
    if "properties" in schema:
        schema["required"] = list(schema["properties"])
    return schema
