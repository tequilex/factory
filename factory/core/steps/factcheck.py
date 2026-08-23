"""text_ready -> factchecked: verify dates, names and numbers in the body."""

from __future__ import annotations

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.models import State
from factory.core.retry import tracked_call
from factory.core.steps import StepContext, StepResult, advanced
from factory.providers.base import FactcheckResult

SYSTEM = (
    "Ты — проверяющий редактор. Проверь в тексте даты, названия, числа и "
    "фактические утверждения. Не переписывай стиль и не сокращай текст. "
    "Верни ответ строго по заданной схеме."
)


@tracked_call(State.TEXT_READY)
def run(ctx: StepContext) -> StepResult:
    if ctx.project.content.factcheck == "off":
        ctx.log.info("фактчек выключен в конфиге", extra={"post_id": ctx.post.id})
        return advanced(State.FACTCHECKED)

    if ctx.post.factcheck_verdict:
        ctx.log.info("фактчек уже выполнен", extra={"post_id": ctx.post.id})
        return advanced(State.FACTCHECKED)

    result = ctx.providers.llm.complete(SYSTEM, ctx.post.body or "", schema=FactcheckResult)

    body = ctx.post.body
    if result.verdict == "fixed" and result.corrected_body:
        body = result.corrected_body

    with db.write_transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE posts SET body = ?, factcheck_verdict = ?, factcheck_notes = ?, "
            "updated_at = ? WHERE id = ?",
            (body, result.verdict, result.notes, to_iso(now_utc()), ctx.post.id),
        )

    if result.verdict == "uncertain":
        # Not a failure: the post goes on, but review will show a warning.
        ctx.log.warning(
            "фактчек не уверен",
            extra={"post_id": ctx.post.id, "notes": result.notes},
        )

    return advanced(State.FACTCHECKED)
