"""Retries and accounting in one place.

Every call that can fail — a step, a provider request — goes through
:func:`tracked_call`. It retries transient failures, respects ``Retry-After`` on
429, and writes one row to ``runs`` describing what happened: how long it took
and what it cost. Without that table there is no answer to "where is the money
going", which the spec requires the owner to be able to ask.

Business logic must not grow its own ``try/except``. Scattered error handling is
how a system ends up retrying something it should not and swallowing something it
should not.
"""

from __future__ import annotations

import functools
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

import httpx

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.errors import ProviderError
from factory.core.logging import get_logger

P = ParamSpec("P")
R = TypeVar("R")

# Attempts inside a single step run. This is the short retry for a flaky network;
# the long one is the state machine's backoff between ticks.
MAX_ATTEMPTS = 3
BACKOFF_BASE_SEC = 2.0

# A provider asking us to wait longer than this is telling us to come back later,
# not to sit in a sleep loop holding the tick lock.
MAX_RETRY_AFTER_SEC = 60.0

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

log = get_logger(__name__)


@dataclass(slots=True)
class Cost:
    """What a call cost. Attach to a result via :func:`with_cost`."""

    usd: float


_COST_ATTR = "_factory_cost_usd"


def with_cost(result: Any, usd: float | None) -> Any:
    """Tag a provider result with its price so ``tracked_call`` can record it.

    ``None`` means the price is unknown — prices live in the project config and
    are optional, because the owner may not care or may not know them. An unknown
    price is recorded as unknown; guessing zero would quietly understate spending.
    """
    if usd is None:
        return result
    try:
        object.__setattr__(result, _COST_ATTR, usd)
    except (AttributeError, TypeError):
        # Immutable results (str, bytes) cannot carry an attribute. Those
        # providers report cost through the context instead.
        log.debug("стоимость не удалось прикрепить к результату", extra={"type": type(result).__name__})
    return result


def cost_of(result: Any) -> float | None:
    """Цена, прикреплённая к результату провайдера, если она известна."""
    return getattr(result, _COST_ATTR, None)


_cost_of = cost_of  # прежнее имя, используется тестами


def _retry_after_sec(exc: BaseException) -> float | None:
    """Seconds the server asked us to wait, if it said so and the value is sane."""
    if isinstance(exc, ProviderError):
        return None if exc.retry_after is None else min(exc.retry_after, MAX_RETRY_AFTER_SEC)
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    raw = exc.response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        # The header also allows an HTTP date. Not worth parsing: providers in
        # practice send seconds, and guessing wrong means sleeping for hours.
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SEC)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    if isinstance(exc, ProviderError):
        # Провайдер, переводящий ответ сервера в понятную человеку ошибку, обязан
        # донести код. Иначе перегрузка на той стороне выглядит окончательным
        # отказом и сжигает попытку поста вместо повтора через минуту.
        return exc.status_code in RETRYABLE_STATUS
    return isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError))


def _idle_wait(result: Any, spent: float | None) -> bool:
    """Шаг посмотрел на пост и решил подождать, ничего не потратив.

    Такие проходы не записываются. Пост на просмотре ждёт человека сутками, а
    воркер смотрит на него раз в минуту — и каждый взгляд ложился в ``runs``
    строкой «ждёт решения». За сутки это десять тысяч записей, лента событий
    превращается в одну повторяющуюся строку, а найти в ней настоящее событие
    нельзя.

    То, что пост ждёт, и так видно по ``posts.state``. А вот потраченные деньги
    записываются всегда, даже если шаг закончился ожиданием: иначе трата
    потеряется.
    """
    return getattr(result, "outcome", None) == "waiting" and not spent


def record_run(
    conn: sqlite3.Connection,
    *,
    step: str,
    ok: bool,
    duration_ms: int,
    post_id: int | None = None,
    cost_usd: float | None = None,
    error: str | None = None,
) -> None:
    """Append one row to ``runs``. Used by :func:`tracked_call` and by tests."""
    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO runs (post_id, step, ok, duration_ms, cost_usd, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (post_id, step, int(ok), duration_ms, cost_usd, error, to_iso(now_utc())),
        )


def tracked_call(
    step: str,
    *,
    conn: sqlite3.Connection | None = None,
    post_id: int | None = None,
    attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap a callable with retries and one ``runs`` row.

    ``conn`` and ``post_id`` may be omitted when the wrapped function receives a
    step context — they are then read off its first argument, which is how the
    pipeline steps use this.

    ``attempts=1`` disables retrying, and irreversible work must use it. A
    publishing call that times out has very likely already succeeded on the far
    side; retrying it posts the same thing to the group a second time. Only
    operations that can be safely repeated get the default.

    ``sleep`` is injectable so tests do not actually wait.
    """

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            connection = conn if conn is not None else _conn_from(args)
            target_post = post_id if post_id is not None else _post_id_from(args)

            started = time.monotonic()
            last_exc: BaseException | None = None

            for attempt in range(1, attempts + 1):
                try:
                    result = func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — re-raised below
                    last_exc = exc
                    if not _is_retryable(exc) or attempt == attempts:
                        break

                    delay = BACKOFF_BASE_SEC ** (attempt - 1)
                    requested = _retry_after_sec(exc)
                    if requested is not None:
                        delay = requested

                    log.warning(
                        "попытка не удалась, повторяю",
                        extra={
                            "step": step,
                            "attempt": attempt,
                            "of": attempts,
                            "delay_sec": delay,
                            "reason": type(exc).__name__,
                        },
                    )
                    sleep(delay)
                    continue

                duration_ms = int((time.monotonic() - started) * 1000)
                spent = _spent_by(args) or cost_of(result)
                if connection is not None and not _idle_wait(result, spent):
                    record_run(
                        connection,
                        step=step,
                        ok=True,
                        duration_ms=duration_ms,
                        post_id=target_post,
                        cost_usd=spent,
                    )
                return result

            duration_ms = int((time.monotonic() - started) * 1000)
            if connection is not None:
                record_run(
                    connection,
                    step=step,
                    ok=False,
                    duration_ms=duration_ms,
                    post_id=target_post,
                    # Провал не значит «бесплатно»: модель берёт деньги и за
                    # ответ, который не прошёл разбор.
                    cost_usd=_spent_by(args) or getattr(last_exc, "cost", None),
                    error=f"{type(last_exc).__name__}: {last_exc}",
                )
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorate


def _conn_from(args: tuple[Any, ...]) -> sqlite3.Connection | None:
    for candidate in args[:1]:
        found = getattr(candidate, "conn", None)
        if isinstance(found, sqlite3.Connection):
            return found
        if isinstance(candidate, sqlite3.Connection):
            return candidate
    return None


def _spent_by(args: tuple[Any, ...]) -> float | None:
    """Сколько шаг потратил на вызовы провайдеров.

    Шаг возвращает StepResult, а цену знает ответ провайдера внутри шага.
    Поэтому стоимость копится в контексте, а не едет с результатом.
    """
    for candidate in args[:1]:
        spent = getattr(candidate, "spent", None)
        if isinstance(spent, (int, float)) and spent > 0:
            return float(spent)
    return None


def _post_id_from(args: tuple[Any, ...]) -> int | None:
    for candidate in args[:1]:
        post = getattr(candidate, "post", None)
        post_id = getattr(post, "id", None)
        if isinstance(post_id, int):
            return post_id
    return None
