"""composed -> in_review -> approved: the human gate.

Two handlers, because the two transitions are different in kind. Sending a post
for review is work the tick does. Waiting for the answer is not work at all — it
returns ``WAITING`` so ``retry_count`` stays untouched, and a post can sit in
review for a week without dying.

In ``auto`` mode both transitions happen without a person. That is the mode used
until the Telegram bot exists in Stage 5.
"""

from __future__ import annotations

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.models import State
from factory.core.retry import tracked_call
from factory.core.steps import StepContext, StepResult, advanced, waiting


def _auto_mode(ctx: StepContext) -> bool:
    return ctx.project.review.mode == "auto"


@tracked_call(State.COMPOSED)
def send_for_review(ctx: StepContext) -> StepResult:
    if _auto_mode(ctx):
        ctx.log.info("ревью в автоматическом режиме", extra={"post_id": ctx.post.id})
        return advanced(State.IN_REVIEW)

    # Stage 5 sends the media group and the keyboard here. Until then the post
    # still moves into in_review and simply waits there.
    ctx.log.info("пост отправлен на ревью", extra={"post_id": ctx.post.id})
    with db.write_transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE posts SET updated_at = ? WHERE id = ?", (to_iso(now_utc()), ctx.post.id)
        )
    return advanced(State.IN_REVIEW)


@tracked_call(State.IN_REVIEW)
def await_decision(ctx: StepContext) -> StepResult:
    if _auto_mode(ctx):
        return advanced(State.APPROVED)

    return waiting("жду решения владельца в Telegram")
