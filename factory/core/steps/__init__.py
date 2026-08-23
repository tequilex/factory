"""The step contract and the registry mapping a state to its handler.

Every transition is one module, named after the state it produces. A handler gets
a :class:`StepContext` and returns a :class:`StepResult`; it must not change
``posts.state`` itself — the state machine does that, in its own transaction,
which is what makes a crash mid-chain safe.

``ADVANCED`` and ``WAITING`` are deliberately different from an exception:

* ``ADVANCED`` — the work is done, move on;
* ``WAITING`` — we are waiting for something outside our control (a person, a
  publishing slot). Not an error: ``retry_count`` must not grow, or a post
  sitting in review over a weekend would burn through its retries and die;
* exception — something went wrong, back off and try again later.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from logging import Logger

from factory.core.config import ProjectConfig
from factory.core.errors import FactoryError
from factory.core.models import TRANSITIONS, Post, State
from factory.providers.registry import Providers


class Outcome(StrEnum):
    ADVANCED = "advanced"
    WAITING = "waiting"


@dataclass(frozen=True, slots=True)
class StepResult:
    outcome: Outcome
    next_state: str | None = None
    reason: str | None = None

    @property
    def advanced(self) -> bool:
        return self.outcome is Outcome.ADVANCED


def advanced(next_state: str) -> StepResult:
    return StepResult(outcome=Outcome.ADVANCED, next_state=next_state)


def waiting(reason: str) -> StepResult:
    return StepResult(outcome=Outcome.WAITING, reason=reason)


@dataclass(slots=True)
class StepContext:
    conn: sqlite3.Connection
    project: ProjectConfig
    post: Post
    providers: Providers
    log: Logger


Handler = Callable[[StepContext], StepResult]


def _registry() -> dict[str, Handler]:
    from factory.core.steps import compose, factcheck, images, prompts, publish, review, text

    return {
        State.QUEUED: text.run,
        State.TEXT_READY: factcheck.run,
        State.FACTCHECKED: prompts.run,
        State.PROMPTS_READY: images.run,
        State.IMAGES_READY: compose.run,
        State.COMPOSED: review.send_for_review,
        State.IN_REVIEW: review.await_decision,
        State.APPROVED: publish.run,
    }


REGISTRY: dict[str, Handler] = _registry()


def handler_for(state: str) -> Handler:
    try:
        return REGISTRY[state]
    except KeyError:
        raise FactoryError(
            f"Для состояния '{state}' нет обработчика.",
            why="Состояние терминальное или в реестре шагов пропущена запись.",
            what_to_do="См. CLAUDE.md → «Как добавить шаг пайплайна».",
        ) from None


# The registry and the transition map describe the same machine from two sides.
# Checked at import so a mismatch fails on startup, not on a post in production.
assert set(REGISTRY) == set(TRANSITIONS), (
    "реестр шагов и карта переходов разошлись: "
    f"{set(REGISTRY) ^ set(TRANSITIONS)}"
)
