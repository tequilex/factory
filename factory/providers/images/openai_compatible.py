"""Any OpenAI-compatible image API: OpenAI itself, RouterAI, another reseller.

Same reasoning as the text provider: ``base_url`` is configurable because the
owner is very likely going through a reseller, and that is the normal case here.

Five things were established by calling the real API before this module existed,
and each one is why some line below looks the way it does:

* the picture arrives inline in ``data[0].b64_json``, not as a link to download;
* the real price of the call is in ``usage.cost`` — better than multiplying a
  price from the config, because reseller prices drift;
* the model name needs its organisation prefix (``black-forest-labs/flux…``);
  without it the answer is 400 "Model not found";
* ``flux.2-klein-4b`` **accepts a reference picture** in the ``image`` field, at
  no extra charge, and that is the whole mechanism behind a recognisable
  character;
* ``gemini-2.5-flash-image`` **silently ignores** that field: HTTP 200, money
  spent, a different woman in the frame. So whether a model honours a reference
  is declared in the config, never guessed from its name.

Retries are not done here: the calling step is already wrapped in
``tracked_call``.
"""

from __future__ import annotations

import base64
import binascii
import io
from typing import Any

import httpx
from PIL import Image

from factory.core import assets, http, paths
from factory.core.errors import FactoryError, ProviderError
from factory.core.logging import get_logger
from factory.core.retry import with_cost
from factory.providers.base import IMAGE_HEIGHT, IMAGE_WIDTH, is_spending_limit

log = get_logger(__name__)


class _Image(bytes):
    """Байты картинки, к которым можно прикрепить стоимость вызова."""


def _advice(status: int, body: str, key_env: str) -> str:
    if status == 401:
        return (
            f"Ключ доступа не принят. Проверь строку {key_env} в файле "
            f"{paths.env_file()}: возможно, скопирован не целиком или отозван."
        )
    if status == 402:
        return (
            "Закончились деньги на балансе провайдера. Пополни счёт — "
            "система продолжит сама, посты ждут в очереди."
        )
    if status == 404:
        return (
            "Адрес или название модели не найдены. Проверь image.model в конфиге "
            "проекта: у большинства моделей имя идёт с названием организации, "
            "например black-forest-labs/flux.2-klein-4b."
        )
    if status == 429:
        return "Слишком много запросов. Система повторит позже сама."
    if 500 <= status < 600:
        return "Сбой на стороне провайдера. Система повторит позже сама."
    return f"Ответ провайдера: {body[:200]}"


def _fit(data: bytes, width: int, height: int) -> bytes:
    """Привести картинку к запрошенному размеру.

    Модель не обязана слушаться поля размера: на запрос 1080×1350 приходит
    1072×1344 — она округляет до кратного 16. Молча отдать чужой размер нельзя:
    сборка обложки растянет кадр под макет, и человек на картинке вытянется.

    Сама обрезка живёт в ``core/assets.py``: тот же код нужен, когда владелец
    приносит свою картинку с телефона, а две реализации одного правила
    разойдутся — и разойдутся молча.
    """
    try:
        return assets.fit(data, width, height)
    except FactoryError as exc:
        raise ProviderError(
            "Провайдер прислал файл, который не открывается как картинка.",
            why=str(exc.why or exc.what),
            what_to_do="Обычно это временный сбой. Система повторит позже.",
        ) from exc


class OpenAICompatibleImages:
    """Генератор картинок через OpenAI-совместимый API."""

    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        key_env: str = "LLM_API_KEY",
        reference: bytes | None = None,
        supports_reference: bool = False,
        price_per_image: float | None = None,
        proxy_env: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.key_env = key_env
        self.api_key = api_key
        self.model = model
        self.supports_reference = supports_reference
        self.price_per_image = price_per_image
        self.proxy_env = proxy_env
        self.calls = 0

        # Образец кодируется один раз при сборке провайдера, а не при каждой
        # картинке: файл один и тот же на весь проект, а base64 от мегабайтного
        # PNG — заметная работа, которую тик делал бы четырежды за пост.
        self.reference_url = (
            "data:image/png;base64," + base64.b64encode(reference).decode()
            if reference and supports_reference
            else None
        )

    def _client(self) -> httpx.Client:
        return http.client_for(
            "images",
            proxy_env=self.proxy_env,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    def generate(
        self,
        prompt: str,
        *,
        lora: str | None = None,
        seed: int | None = None,
        width: int = IMAGE_WIDTH,
        height: int = IMAGE_HEIGHT,
    ) -> bytes:
        self.calls += 1

        if lora:
            # Тихо проигнорировать было бы худшим исходом: владелец платил бы за
            # дообученную модель и получал чужого человека, не понимая почему.
            raise ProviderError(
                "Этот провайдер не умеет работать с дообученной моделью (LoRA).",
                why=(
                    f"В конфиге задано image.lora: {lora}, но реселлер с "
                    "OpenAI-совместимым API раздаёт общие для всех веса, и "
                    "приложить к ним свой файл некуда."
                ),
                what_to_do=(
                    "Убери строку image.lora из конфига проекта. Как подключить "
                    "дообучение — docs/PLAN-этап-4.md, раздел «Что отложено»."
                ),
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": f"{width}x{height}",
        }
        if seed is not None:
            payload["seed"] = seed
        if self.reference_url:
            payload["image"] = self.reference_url

        with self._client() as client:
            response = client.post(f"{self.base_url}/images/generations", json=payload)

        if response.status_code != 200:
            self._raise_for_status(response)

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                "Провайдер вернул не-JSON вместо картинки.",
                why=f"Первые символы: {response.text[:120]!r}",
                what_to_do="Обычно это временный сбой. Система повторит позже.",
            ) from exc

        raw = self._decode(data)
        cost = self._cost(data)

        log.info(
            "картинка получена",
            extra={"model": self.model, "bytes": len(raw), "cost": cost,
                   "reference": bool(self.reference_url)},
        )
        return with_cost(_Image(_fit(raw, width, height)), cost)

    def _raise_for_status(self, response: httpx.Response) -> None:
        body = response.text
        if response.status_code == 429 and is_spending_limit(body):
            # Не ошибка сети и не «слишком часто»: месячный потолок ключа снимает
            # человек в личном кабинете. Ретраить это значит жечь попытки поста в
            # ожидании события, которое без владельца не наступит, — ровно то же
            # правило, что и для истёкшего ключа ВК.
            raise ProviderError(
                "Исчерпан лимит расходов ключа у провайдера картинок.",
                why=body.strip()[:200],
                what_to_do=(
                    "Подними месячный лимит ключа в личном кабинете провайдера "
                    f"({self.base_url}) или заведи новый ключ и пропиши его в "
                    f"{paths.env_file()}. Посты подождут, ничего не потеряется."
                ),
                needs_human=True,
            )

        raise ProviderError(
            f"Картинка не сгенерировалась: код {response.status_code}.",
            why=f"Модель {self.model}, адрес {self.base_url}.",
            what_to_do=_advice(response.status_code, body, self.key_env),
            status_code=response.status_code,
        )

    def _decode(self, data: dict) -> bytes:
        try:
            encoded = data["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                "В ответе провайдера нет картинки.",
                why=f"Пришло: {str(data)[:200]}",
                what_to_do=(
                    "Проверь image.model в конфиге проекта: не всякая модель "
                    "умеет рисовать картинки, а некоторые отдают ссылку вместо "
                    "файла и здесь не подойдут."
                ),
            ) from exc

        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderError(
                "Картинка от провайдера не раскодировалась.",
                why=f"Поле b64_json длиной {len(encoded)} не является base64.",
                what_to_do="Обычно это временный сбой. Система повторит позже.",
            ) from exc

    def _cost(self, data: dict) -> float | None:
        """Цена вызова: сначала фактическая из ответа, иначе из конфига.

        Настоящая цена лучше расчётной: у реселлеров она плавает от модели к
        модели и от размера к размеру, и своя арифметика однажды разойдётся со
        счётом — причём в меньшую сторону, то есть незаметно.
        """
        usage = data.get("usage") or {}
        reported = usage.get("cost")
        if isinstance(reported, (int, float)):
            return float(reported)
        return self.price_per_image
