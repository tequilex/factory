"""text_ready -> factchecked: verify dates, names and numbers in the body.

Uses its own model, not the one that wrote the text. That separation is not
tidiness — it was proved necessary. Given a post claiming a fine of 50 000 ₽ when
the real figure is 500 ₽, a model without web search answered ``ok`` and cited a
regulation that says nothing of the sort. A check that confidently approves a
hundredfold error is worse than no check at all: it stamps "verified" on a lie.

So ``content.factcheck`` means different things:

* ``strict`` — a model with web search. The config refuses to start without one;
* ``light`` — no search: only internal contradictions and obvious nonsense are
  caught, and the verdict says so;
* ``off`` — skipped entirely.
"""

from __future__ import annotations

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.models import State
from factory.core.retry import tracked_call
from factory.core.steps import StepContext, StepResult, advanced
from factory.providers.base import FactcheckResult

SYSTEM_STRICT = (
    "Ты — проверяющий редактор. Проверь в тексте даты, числа, названия, суммы "
    "и фактические утверждения по источникам в интернете.\n\n"
    "Если нашёл ошибку — исправь её в тексте и верни исправленный вариант "
    "целиком, сохранив стиль и объём. Не переписывай то, что верно, не сокращай "
    "и не меняй интонацию.\n\n"
    "verdict: ok — всё верно; fixed — были ошибки, исправил; uncertain — не "
    "смог проверить.\n"
    "notes: коротко, что именно проверил и что исправил."
)

SYSTEM_LIGHT = (
    "Ты — проверяющий редактор без доступа к интернету. Проверь текст только на "
    "внутренние противоречия и очевидные несообразности.\n\n"
    "Ты НЕ можешь подтвердить факты по источникам. Поэтому verdict: ok ставь "
    "только если утверждать нечего — в тексте нет проверяемых фактов. Если "
    "факты есть, но проверить их нельзя, ставь uncertain.\n"
    "fixed — только для явных внутренних противоречий."
)

# Приписка к заметкам, когда проверки по источникам не было. Владелец должен
# видеть это в ревью, иначе «проверено» вводит в заблуждение.
NO_SEARCH_NOTE = "Проверка без поиска по источникам: факты не подтверждены."


@tracked_call(State.TEXT_READY)
def run(ctx: StepContext) -> StepResult:
    mode = ctx.project.content.factcheck

    if mode == "off":
        ctx.log.info("фактчек выключен в конфиге", extra={"post_id": ctx.post.id})
        return advanced(State.FACTCHECKED)

    if ctx.post.factcheck_verdict:
        ctx.log.info("фактчек уже выполнен", extra={"post_id": ctx.post.id})
        return advanced(State.FACTCHECKED)

    with_search = ctx.project.llm.factcheck_web_search
    system = SYSTEM_STRICT if with_search else SYSTEM_LIGHT

    result = ctx.charge(
        ctx.providers.factcheck.complete(system, ctx.post.body or "", schema=FactcheckResult)
    )

    body = ctx.post.body
    if result.verdict == "fixed" and result.corrected_body:
        body = result.corrected_body

    notes = result.notes or ""
    if not with_search:
        notes = f"{NO_SEARCH_NOTE} {notes}".strip()

    with db.write_transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE posts SET body = ?, factcheck_verdict = ?, factcheck_notes = ?, "
            "updated_at = ? WHERE id = ?",
            (body, result.verdict, notes, to_iso(now_utc()), ctx.post.id),
        )

    if result.verdict == "uncertain":
        # Не ошибка: пост едет дальше, но в ревью показывается с предупреждением.
        ctx.log.warning(
            "фактчек не уверен",
            extra={"post_id": ctx.post.id, "notes": notes[:200]},
        )
    elif result.verdict == "fixed":
        ctx.log.info(
            "фактчек исправил текст",
            extra={"post_id": ctx.post.id, "notes": notes[:200]},
        )

    return advanced(State.FACTCHECKED)
