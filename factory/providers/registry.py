"""Turning a provider name from the config into an object.

The names a config may use are declared in ``core/config.py``; the code behind
them lives here. A test asserts the two lists agree, so a name that validates but
has no implementation cannot reach production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from factory.core.config import IMAGE_PROVIDERS, LLM_PROVIDERS, PUBLISHER_PROVIDERS, ProjectConfig
from factory.core.errors import ConfigError
from factory.providers.base import ImageProvider, LLMProvider, Publisher


@dataclass(slots=True)
class Providers:
    """The three services a step may reach for. Built once per project per tick."""

    llm: LLMProvider
    images: ImageProvider
    publisher: Publisher


def _stub_llm(config: ProjectConfig):
    from factory.providers.llm.stub import StubLLM

    return StubLLM(model=config.llm.model)


def _stub_images(config: ProjectConfig):
    from factory.providers.images.stub import StubImages

    return StubImages(model=config.image.model)


def _stub_publisher(config: ProjectConfig):
    from factory.providers.publishers.stub import StubPublisher

    return StubPublisher(group_id=config.vk.group_id)


# Real implementations arrive in later stages; the names are already accepted by
# the config so a project can be written before its provider exists.
LLM_BUILDERS: dict[str, Callable[[ProjectConfig], LLMProvider]] = {
    "stub": _stub_llm,
}

IMAGE_BUILDERS: dict[str, Callable[[ProjectConfig], ImageProvider]] = {
    "stub": _stub_images,
}

PUBLISHER_BUILDERS: dict[str, Callable[[ProjectConfig], Publisher]] = {
    "stub": _stub_publisher,
}

_STAGE_OF = {
    "openai_compatible": "Этапе 3",
    "anthropic": "Этапе 3",
    "replicate": "Этапе 4",
    "vk": "Этапе 2",
}


def _build(kind: str, name: str, builders: dict, allowed: tuple[str, ...], config: ProjectConfig):
    builder = builders.get(name)
    if builder is not None:
        return builder(config)

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
    """Instantiate all three providers a project needs."""
    return Providers(
        llm=_build("llm", config.llm.provider, LLM_BUILDERS, LLM_PROVIDERS, config),
        images=_build("image", config.image.provider, IMAGE_BUILDERS, IMAGE_PROVIDERS, config),
        publisher=_build(
            "publisher", config.publisher.provider, PUBLISHER_BUILDERS, PUBLISHER_PROVIDERS, config
        ),
    )
