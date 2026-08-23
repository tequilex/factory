"""Fake LLM. Deterministic, free, offline.

Deterministic on purpose: the same input always gives the same output, so a test
can assert on the result and a crash-resume test can tell "regenerated" from
"restored". Randomness here would make idempotence untestable.

Nothing here may know what the project writes about. A stub with cooking topics
baked in would produce cooking placeholders for a car community — and, worse,
would be a piece of niche knowledge living in ``providers/``, which the spec
forbids. The text is therefore built from the topic the caller passed in, and the
scenes describe only the recurring character, never a subject.
"""

from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel

from factory.providers.base import TITLE_MAX_LENGTH, FactcheckResult, PostDraft, ScenePrompts

# Шаг text кладёт тему первой строкой запроса. Заглушка её оттуда достаёт, чтобы
# посты отличались друг от друга и было видно, какой пост про что.
TOPIC_RE = re.compile(r"^Тема:\s*(.+)$", re.MULTILINE)

# Сцены описывают только персонажа и свет — ничего о тематике сообщества.
SCENES = [
    "close-up portrait of the recurring character, soft daylight",
    "the character seated by a window, shallow depth of field",
    "the character walking outdoors, overcast light",
    "the character holding a cup, warm indoor light",
    "the character looking away from camera, evening light",
]


def _topic_of(user_prompt: str) -> str:
    match = TOPIC_RE.search(user_prompt)
    return match.group(1).strip() if match else "Без темы"


def _title_for(topic: str) -> str:
    """Заголовок для обложки: короткий, без точки в конце."""
    title = topic.rstrip(" .!?")
    if len(title) <= TITLE_MAX_LENGTH:
        return title
    return title[: TITLE_MAX_LENGTH - 1].rstrip() + "…"


def _rotate(options: list[str], seed_text: str) -> list[str]:
    """Стабильный порядок вариантов, зависящий от содержимого запроса."""
    shift = hashlib.sha256(seed_text.encode("utf-8")).digest()[0] % len(options)
    return options[shift:] + options[:shift]


class StubLLM:
    """Заглушка LLM: возвращает правдоподобные данные нужной формы."""

    name = "stub"

    def __init__(self, *, model: str = "stub") -> None:
        self.model = model
        self.calls = 0

    def complete(
        self, system: str, user: str, *, schema: type[BaseModel] | None = None
    ) -> str | BaseModel:
        self.calls += 1

        if schema is PostDraft:
            topic = _topic_of(user)
            return PostDraft(
                title=_title_for(topic),
                body=(
                    f"Тема этого поста: {topic}.\n\n"
                    "Текст — заглушка. Настоящий появится, когда будет подключён "
                    "реальный провайдер: сейчас важно только то, что пост "
                    "проезжает всю цепочку состояний и переживает перезапуск.\n\n"
                    "Второй абзац нужен для объёма, чтобы обложка и разметка "
                    "проверялись на тексте реалистичной длины."
                ),
                question="А у вас так же или только у меня?",
            )

        if schema is FactcheckResult:
            return FactcheckResult(verdict="ok", corrected_body=None, notes=None)

        if schema is ScenePrompts:
            scenes = _rotate(SCENES, user)
            return ScenePrompts(cover=scenes[0], inline=scenes[1:])

        return f"stub-ответ на запрос длиной {len(user)} символов"
