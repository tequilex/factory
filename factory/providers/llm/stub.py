"""Fake LLM. Deterministic, free, offline.

Deterministic on purpose: the same input always gives the same output, so a test
can assert on the result and a crash-resume test can tell "regenerated" from
"restored". Randomness here would make idempotence untestable.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from factory.providers.base import FactcheckResult, PostDraft, ScenePrompts

# Заголовки заведомо короче 60 символов и без точки в конце — как требует спека.
TITLES = [
    "Три года ездила и не знала",
    "Оказалось, всё было проще",
    "Проверила на неделе — работает",
    "Почему все светофоры красные",
    "Нашла случайно, стоя в пробке",
]

SCENES = [
    "young woman looking out of a car window, city street",
    "close-up of hands on a steering wheel, morning light",
    "woman walking away from a parked car, autumn park",
    "coffee cup on a dashboard, rain on the windshield",
]


def _pick(options: list[str], seed_text: str) -> str:
    """Стабильный выбор из списка по содержимому запроса."""
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    return options[digest[0] % len(options)]


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
        seed_text = f"{system}\n{user}"

        if schema is PostDraft:
            title = _pick(TITLES, seed_text)
            return PostDraft(
                title=title,
                body=(
                    f"{title}.\n\n"
                    "Это текст-заглушка. Настоящий появится на Этапе 3, когда "
                    "подключим реальный LLM-провайдер. Пока он нужен только "
                    "чтобы проверить, что пост доезжает по всей цепочке "
                    "состояний и переживает перезапуск.\n\n"
                    "Второй абзац для объёма, чтобы обложка и разметка "
                    "проверялись на тексте реалистичной длины."
                ),
                question="А у вас так же или только у меня?",
            )

        if schema is FactcheckResult:
            return FactcheckResult(verdict="ok", corrected_body=None, notes=None)

        if schema is ScenePrompts:
            cover = _pick(SCENES, seed_text)
            rest = [scene for scene in SCENES if scene != cover]
            return ScenePrompts(cover=cover, inline=rest)

        return f"stub-ответ на запрос длиной {len(user)} символов"
