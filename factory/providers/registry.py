"""Turning a provider name from the config into an object.

The names a config may use are declared in ``core/config.py``; the code behind
them lives here. A test asserts the two lists agree, so a name that validates but
has no implementation cannot reach production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from factory.core.config import (
    IMAGE_PROVIDERS,
    LLM_PROVIDERS,
    NOTIFIER_PROVIDERS,
    PUBLISHER_PROVIDERS,
    ProjectConfig,
)
from factory.core.errors import ConfigError
from factory.providers.base import ImageProvider, LLMProvider, Notifier, Publisher


@dataclass(slots=True)
class Providers:
    """The services a step may reach for. Built once per project per tick.

    ``factcheck`` is a separate object from ``llm`` on purpose. Checking facts and
    writing prose are different jobs: proved live that a model without web search
    approved a text where a fine was overstated a hundredfold, and cited a
    non-existent regulation while doing it. The cheap model writes; a model that
    can look things up checks.
    """

    llm: LLMProvider
    images: ImageProvider
    publisher: Publisher
    factcheck: LLMProvider
    notifier: Notifier


def _stub_llm(config: ProjectConfig, *, factcheck: bool = False):
    from factory.providers.llm.stub import StubLLM

    return StubLLM(model=config.llm.model)


def _openai_compatible(config: ProjectConfig, *, factcheck: bool = False):
    from factory.core.config import resolve_secret
    from factory.providers.llm.openai_compatible import OpenAICompatibleLLM

    llm = config.llm
    role = "фактчека" if factcheck else "написания текстов"
    model = (llm.factcheck_model or llm.model) if factcheck else llm.model
    prices = (
        (llm.factcheck_price_input_per_1m, llm.factcheck_price_output_per_1m)
        if factcheck
        else (llm.price_input_per_1m, llm.price_output_per_1m)
    )

    return OpenAICompatibleLLM(
        base_url=resolve_secret(llm.base_url_env, context=f"адреса API для {role}"),
        api_key=resolve_secret(llm.api_key_env, context=f"модели {role}"),
        key_env=llm.api_key_env,
        model=model,
        max_tokens=llm.max_tokens,
        temperature=llm.temperature,
        price_input_per_1m=prices[0],
        price_output_per_1m=prices[1],
        proxy_env=llm.proxy_env,
    )


def _stub_notifier(config: ProjectConfig):
    from factory.providers.notifiers.stub import StubNotifier

    return StubNotifier()


def _telegram_notifier(config: ProjectConfig):
    from factory.core.config import resolve_secret
    from factory.providers.notifiers.telegram import TelegramNotifier

    telegram = config.telegram
    return TelegramNotifier(
        token=resolve_secret(telegram.token_env, context="бота в Telegram"),
        token_env=telegram.token_env,
        proxy_env=telegram.proxy_env,
    )


def _stub_images(config: ProjectConfig):
    from factory.providers.images.stub import StubImages

    return StubImages(model=config.image.model)


def _openai_compatible_images(config: ProjectConfig):
    from factory.core.config import resolve_secret
    from factory.providers.images.openai_compatible import OpenAICompatibleImages

    image = config.image
    # Файл читается здесь, при сборке провайдера, а не при каждой картинке:
    # он один на весь проект, и пропажу лучше заметить на старте тика, чем
    # четырьмя платными вызовами позже.
    reference = (
        config.reference_path.read_bytes()
        if config.reference_path is not None and config.reference_path.is_file()
        else None
    )

    return OpenAICompatibleImages(
        base_url=resolve_secret(image.base_url_env, context="адреса API для картинок"),
        api_key=resolve_secret(image.api_key_env, context="модели картинок"),
        key_env=image.api_key_env or "",
        model=image.model,
        reference=reference,
        supports_reference=image.supports_reference,
        price_per_image=image.price_per_image,
        proxy_env=image.proxy_env,
    )


def _stub_publisher(config: ProjectConfig):
    from factory.providers.publishers.stub import StubPublisher

    return StubPublisher(group_id=config.vk.group_id)


def _vk_publisher(config: ProjectConfig):
    from factory.core.config import resolve_secret
    from factory.providers.publishers.vk import VkPublisher

    # Два ключа. Оба обязательны: конфиг это уже проверил, но секреты
    # разрешаются здесь — их отсутствие должно быть видно при сборке
    # провайдера, а не на последнем шаге пайплайна.
    return VkPublisher(
        group_id=config.vk.group_id,
        token=resolve_secret(
            config.vk.token_env, context=f"публикации в группу проекта {config.slug}"
        ),
        upload_token=resolve_secret(
            config.vk.upload_token_env,
            context=f"загрузки картинок в ВК для проекта {config.slug}",
        ),
        # Имена переменных нужны, чтобы «ошибка 5» отвечала, какой из двух
        # ключей менять: картинки грузит один, публикует другой.
        token_env=config.vk.token_env,
        upload_token_env=config.vk.upload_token_env,
        api_version=config.vk.api_version,
        proxy_env=config.vk.proxy_env,
    )


# Real implementations arrive in later stages; the names are already accepted by
# the config so a project can be written before its provider exists.
LLM_BUILDERS: dict[str, Callable[..., LLMProvider]] = {
    "stub": _stub_llm,
    "openai_compatible": _openai_compatible,
}

IMAGE_BUILDERS: dict[str, Callable[[ProjectConfig], ImageProvider]] = {
    "stub": _stub_images,
    "openai_compatible": _openai_compatible_images,
}

PUBLISHER_BUILDERS: dict[str, Callable[[ProjectConfig], Publisher]] = {
    "stub": _stub_publisher,
    "vk": _vk_publisher,
}

NOTIFIER_BUILDERS: dict[str, Callable[[ProjectConfig], Notifier]] = {
    "stub": _stub_notifier,
    "telegram": _telegram_notifier,
}

_STAGE_OF = {
    "anthropic": "Этапе 3",
    "replicate": "Этапе 4",
}


def _build(
    kind: str,
    name: str,
    builders: dict,
    allowed: tuple[str, ...],
    config: ProjectConfig,
    **extra,
):
    builder = builders.get(name)
    if builder is not None:
        return builder(config, **extra) if extra else builder(config)

    if name in allowed:
        stage = _STAGE_OF.get(name, "одном из следующих этапов")
        raise ConfigError(
            f"Провайдер {kind} '{name}' пока не реализован.",
            why=f"Он появится на {stage}. Сейчас доступен только 'stub'.",
            what_to_do=(
                f"Поставь {kind}.provider: stub в конфиге проекта '{config.slug}' "
                "и вернись к этой настройке, когда этап будет готов."
            ),
        )

    raise ConfigError(
        f"Неизвестный провайдер {kind}: '{name}'.",
        why=f"Доступные значения: {', '.join(allowed)}.",
        what_to_do=f"Исправь поле {kind}.provider в конфиге проекта '{config.slug}'.",
    )


def build_providers(config: ProjectConfig) -> Providers:
    """Instantiate every provider a project needs."""
    llm_name = config.llm.provider
    # Без секции telegram уведомлять некуда и незачем: конфиг это допускает
    # только при review.mode: auto, где владельца ни о чём не спрашивают.
    notifier_name = config.telegram.provider if config.telegram else "stub"
    return Providers(
        llm=_build("llm", llm_name, LLM_BUILDERS, LLM_PROVIDERS, config),
        # Отдельный объект: у фактчека своя модель и свои цены.
        factcheck=_build(
            "llm", llm_name, LLM_BUILDERS, LLM_PROVIDERS, config, factcheck=True
        ),
        images=_build("image", config.image.provider, IMAGE_BUILDERS, IMAGE_PROVIDERS, config),
        publisher=_build(
            "publisher", config.publisher.provider, PUBLISHER_BUILDERS, PUBLISHER_PROVIDERS, config
        ),
        notifier=_build(
            "telegram", notifier_name, NOTIFIER_BUILDERS, NOTIFIER_PROVIDERS, config
        ),
    )
