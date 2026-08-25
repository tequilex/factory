"""Post states and typed views over database rows.

A post is a state machine. The map below is the single definition of its shape:
:data:`TRANSITIONS` drives the step registry, and the ``CHECK`` constraint in
``001_init.sql`` mirrors :class:`State`. ``test_models.py`` asserts the two lists
still agree — if they drift, a step fails with an ``IntegrityError`` in production
rather than in a test.

Row wrappers are plain dataclasses, not an ORM: they convert timestamps to aware
datetimes at the boundary and otherwise stay out of the way.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from factory.core.clock import from_iso
from factory.core.errors import FactoryError


class State(StrEnum):
    QUEUED = "queued"
    TEXT_READY = "text_ready"
    FACTCHECKED = "factchecked"
    PROMPTS_READY = "prompts_ready"
    IMAGES_READY = "images_ready"
    COMPOSED = "composed"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    PUBLISHED = "published"
    FAILED = "failed"
    REJECTED = "rejected"


class TopicStatus(StrEnum):
    FREE = "free"
    TAKEN = "taken"
    USED = "used"


class AssetKind(StrEnum):
    COVER = "cover"
    INLINE = "inline"


class RejectionReason(StrEnum):
    TEXT = "text"
    # «Сцены придуманы плохо» — в отличие от IMAGES «сцены хорошие, нарисовано
    # плохо». Для будущего обучающего набора это разные сигналы: первый учит
    # придумывать, второй — рисовать.
    SCENES = "scenes"
    IMAGES = "images"
    TRASH = "trash"


TERMINAL_STATES: frozenset[State] = frozenset(
    {State.PUBLISHED, State.FAILED, State.REJECTED}
)

TRANSITIONS: dict[State, State] = {
    State.QUEUED: State.TEXT_READY,
    State.TEXT_READY: State.FACTCHECKED,
    State.FACTCHECKED: State.PROMPTS_READY,
    State.PROMPTS_READY: State.IMAGES_READY,
    State.IMAGES_READY: State.COMPOSED,
    State.COMPOSED: State.IN_REVIEW,
    State.IN_REVIEW: State.APPROVED,
    State.APPROVED: State.PUBLISHED,
}


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def next_state(state: str) -> State:
    """The state a successful step moves a post into."""
    try:
        current = State(state)
    except ValueError:
        known = ", ".join(sorted(State))
        raise FactoryError(
            f"Неизвестное состояние поста: '{state}'.",
            why="Такого состояния нет в стейт-машине — скорее всего, опечатка.",
            what_to_do=f"Допустимые состояния: {known}.",
        ) from None

    if current in TERMINAL_STATES:
        raise FactoryError(
            f"Состояние '{state}' терминальное, дальше двигаться некуда.",
            why="Пост уже опубликован, отклонён или помечен как сломанный.",
            what_to_do=(
                "Чтобы вернуть пост в работу, используй factory post retry <id>. "
                "См. RUNBOOK.md → «Когда сломалось»."
            ),
        )

    return TRANSITIONS[current]


class _Row:
    """Mixin building a dataclass from a ``sqlite3.Row``.

    Column names that are also field names are copied across; anything listed in
    ``_TIMESTAMPS`` is parsed into an aware UTC datetime, and ``_FLAGS`` becomes a
    real ``bool`` instead of SQLite's 0/1 integer.
    """

    _TIMESTAMPS: ClassVar[frozenset[str]] = frozenset()
    _FLAGS: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def from_row(cls, row: sqlite3.Row):
        names = {field.name for field in dataclasses.fields(cls)}
        values: dict[str, Any] = {}
        for column in row.keys():
            if column not in names:
                continue
            value = row[column]
            if value is not None and column in cls._TIMESTAMPS:
                value = from_iso(value)
            elif column in cls._FLAGS:
                value = bool(value)
            values[column] = value
        return cls(**values)


@dataclass(slots=True)
class Project(_Row):
    id: int
    slug: str
    config_path: str
    created_at: datetime
    is_active: bool = True

    _TIMESTAMPS: ClassVar[frozenset[str]] = frozenset({"created_at"})
    _FLAGS: ClassVar[frozenset[str]] = frozenset({"is_active"})


@dataclass(slots=True)
class Topic(_Row):
    id: int
    project_id: int
    title: str
    angle: str | None = None
    status: str = TopicStatus.FREE
    used_at: datetime | None = None

    _TIMESTAMPS: ClassVar[frozenset[str]] = frozenset({"used_at"})


@dataclass(slots=True)
class Post(_Row):
    id: int
    project_id: int
    topic_id: int
    idem_key: str
    state: str
    created_at: datetime
    updated_at: datetime
    title: str | None = None
    body: str | None = None
    question: str | None = None
    factcheck_verdict: str | None = None
    factcheck_notes: str | None = None
    retry_count: int = 0
    last_error: str | None = None
    next_attempt_at: datetime | None = None
    scheduled_at: datetime | None = None
    external_id: str | None = None
    published_at: datetime | None = None
    publish_guid: str | None = None
    review_chat_id: int | None = None
    review_album_at: datetime | None = None
    review_message_id: int | None = None
    decided_at: datetime | None = None
    decided_by: int | None = None

    _TIMESTAMPS: ClassVar[frozenset[str]] = frozenset(
        {"created_at", "updated_at", "next_attempt_at", "scheduled_at", "published_at", "decided_at", "review_album_at"}
    )

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.state)


@dataclass(slots=True)
class Asset(_Row):
    id: int
    post_id: int
    kind: str
    position: int
    created_at: datetime
    prompt: str | None = None
    seed: int | None = None
    local_path: str | None = None
    external_ref: str | None = None

    _TIMESTAMPS: ClassVar[frozenset[str]] = frozenset({"created_at"})


@dataclass(slots=True)
class Run(_Row):
    id: int
    step: str
    ok: bool
    created_at: datetime
    post_id: int | None = None
    duration_ms: int | None = None
    cost_usd: float | None = None
    error: str | None = None

    _TIMESTAMPS: ClassVar[frozenset[str]] = frozenset({"created_at"})
    _FLAGS: ClassVar[frozenset[str]] = frozenset({"ok"})


@dataclass(slots=True)
class Rejection(_Row):
    id: int
    post_id: int
    reason: str
    created_at: datetime
    snapshot: str | None = None

    _TIMESTAMPS: ClassVar[frozenset[str]] = frozenset({"created_at"})
