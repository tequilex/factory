"""The four protocols every external service hides behind.

Nothing outside ``providers/`` knows whether text comes from OpenAI or from a
stub, whether images come from Replicate or from Pillow, whether a post lands in
VK or in a file. Steps depend on these protocols only, which is what makes
"switch to the real provider" a one-line config change rather than a rewrite.

The pydantic models below are the structured shapes steps ask an LLM for. They
are part of the contract: a provider that cannot produce them is not usable here.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

# Every image is generated at this size — the VK feed crops anything else badly.
IMAGE_WIDTH = 1080
IMAGE_HEIGHT = 1350

TITLE_MAX_LENGTH = 60


class PostDraft(BaseModel):
    """What the ``text`` step asks the LLM for."""

    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)
    body: str = Field(min_length=1)
    question: str = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def _no_trailing_period(cls, value: str) -> str:
        """SPEC.md: заголовок для обложки — без точки в конце."""
        return value.rstrip().rstrip(".")


class FactcheckResult(BaseModel):
    """What the ``factcheck`` step asks the LLM for."""

    verdict: str = Field(pattern="^(ok|fixed|uncertain)$")
    corrected_body: str | None = None
    notes: str | None = None


class ScenePrompts(BaseModel):
    """What the ``prompts`` step asks the LLM for. English, one cover plus inlines."""

    cover: str = Field(min_length=1)
    inline: list[str] = Field(default_factory=list)


@runtime_checkable
class LLMProvider(Protocol):
    def complete(
        self, system: str, user: str, *, schema: type[BaseModel] | None = None
    ) -> str | BaseModel: ...


@runtime_checkable
class ImageProvider(Protocol):
    def generate(
        self,
        prompt: str,
        *,
        lora: str | None = None,
        seed: int | None = None,
        width: int = IMAGE_WIDTH,
        height: int = IMAGE_HEIGHT,
    ) -> bytes: ...


class ReviewMessage(BaseModel):
    """Куда легло отправленное на ревью сообщение.

    Идентификаторы нужны, чтобы после решения убрать кнопки: сообщение с живой
    клавиатурой, по которой уже нажали, — приглашение нажать ещё раз.
    """

    chat_id: int
    message_id: int


@runtime_checkable
class Notifier(Protocol):
    """Односторонняя связь с владельцем: посты на одобрение и тревоги.

    Отправкой занимается воркер, а не бот: не ушло сообщение — пост остаётся на
    прежнем шаге и попробует снова. Иначе система считала бы, что спросила
    владельца, а телефон при этом молчал.
    """

    def send_for_review(
        self, *, chat_id: int, project: str, title: str, body: str,
        warning: str | None, images: list[str], post_id: int,
    ) -> ReviewMessage: ...

    def alert(self, *, chat_id: int, text: str) -> None: ...

    def close_review(self, *, chat_id: int, message_id: int, verdict: str) -> None: ...


@runtime_checkable
class Publisher(Protocol):
    def publish(self, post, assets) -> str: ...

    def fetch_comments(self, external_id: str) -> list: ...

    def reply(self, external_comment_id: str, text: str) -> None: ...
