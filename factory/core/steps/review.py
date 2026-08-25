"""composed -> in_review -> approved: the human gate.

Two handlers, because the two transitions are different in kind. Sending a post
for review is work the tick does. Waiting for the answer is not work at all — it
returns ``WAITING`` so ``retry_count`` stays untouched, and a post can sit in
review for a week without dying.

Sending happens here, in the worker, over plain HTTP — not in the bot. The bot is
asynchronous and the worker is not, and mixing the two in one process buys
nothing but hangs that never reproduce. It also means a post only reaches
``in_review`` if the message actually went out. The alternative — the bot
sending — would let posts pile up invisibly whenever the bot was down.

**No retries here**, for the same reason publishing has none: a timeout does not
mean the message failed to arrive. Proved live — the owner got the same album of
four images three times, because the reply from Telegram was slow and each retry
uploaded everything again. Telegram offers no idempotency key, so the choice is
per-call: at-most-once or at-least-once.

They are decided differently, because the failures are not equally bad:

* **the album — at most once.** ``review_album_at`` is written *before* the call.
  A repeat is visible spam the owner cannot ignore; a missing album is a post
  whose text still arrived with working buttons;
* **the text with the keyboard — at least once.** Losing it strands the post
  forever with no way to answer. A duplicate keyboard is only cosmetic: pressing
  the older one answers "решение уже принято" and changes nothing.
"""

from __future__ import annotations

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.decisions import approvals_in_a_row
from factory.core.models import State
from factory.core.retry import tracked_call
from factory.core.steps import StepContext, StepResult, advanced, waiting

# Что показать владельцу, когда фактчек не был уверен или что-то исправил.
FACTCHECK_NOTE = {
    "uncertain": "Фактчек не уверен",
    "fixed": "Фактчек исправил текст",
}


def _skips_review(ctx: StepContext) -> str | None:
    """Причина пропустить ревью, или ``None`` — спрашиваем человека."""
    if ctx.project.review.mode == "auto":
        return "режим auto в конфиге"

    needed = ctx.project.review.auto_after_n_approved
    streak = approvals_in_a_row(ctx.conn, ctx.post.project_id)
    if streak >= needed:
        return f"подряд одобрено {streak} постов без правок"
    return None


def _warning(ctx: StepContext) -> str | None:
    verdict = ctx.post.factcheck_verdict
    label = FACTCHECK_NOTE.get(verdict or "")
    if not label:
        return None
    notes = (ctx.post.factcheck_notes or "").strip()
    return f"{label}: {notes}" if notes else label


def _image_paths(ctx: StepContext) -> list[str]:
    """Обложка первой, дальше по порядку — так владелец видит главное сразу."""
    rows = ctx.conn.execute(
        "SELECT local_path FROM assets WHERE post_id = ? AND local_path IS NOT NULL "
        "ORDER BY CASE kind WHEN 'cover' THEN 0 ELSE 1 END, position",
        (ctx.post.id,),
    ).fetchall()
    return [row["local_path"] for row in rows]


def _mark_album(ctx: StepContext, message_id: int | None) -> None:
    """Запомнить, что картинки уже отправляли. Своей транзакцией."""
    with db.write_transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE posts SET review_album_at = ?, review_album_message_id = ? WHERE id = ?",
            (to_iso(now_utc()), message_id, ctx.post.id),
        )


def _send_album(ctx: StepContext, chat_id: int) -> int | None:
    """Отправить картинки не больше одного раза, но и не потерять их зря.

    Отметка ставится по итогу, а не заранее, и решает его одна вещь: мог ли
    альбом дойти. Соединение не установилось — не мог, отметку ставить нельзя,
    иначе пост придёт к владельцу вовсе без картинок, а решать ему по ним.
    Ответа не дождались — мог, и повтор прислал бы второй альбом.
    """
    images = _image_paths(ctx)
    if not images:
        _mark_album(ctx, None)
        return None

    try:
        album_id = ctx.providers.notifier.send_album(
            chat_id=chat_id,
            caption=f"[{ctx.project.slug}] {ctx.post.title or ''}",
            images=images,
        )
    except Exception as exc:  # noqa: BLE001 — разбираем и пробрасываем дальше
        if getattr(exc, "delivered_unknown", True):
            _mark_album(ctx, None)
            ctx.log.warning(
                "судьба альбома неизвестна, второй раз не отправляем",
                extra={"post_id": ctx.post.id},
            )
        else:
            ctx.log.info(
                "альбом не ушёл, попробуем целиком позже", extra={"post_id": ctx.post.id}
            )
        raise

    _mark_album(ctx, album_id)
    return album_id


@tracked_call(State.COMPOSED, attempts=1)
def send_for_review(ctx: StepContext) -> StepResult:
    reason = _skips_review(ctx)
    if reason:
        ctx.log.info("ревью пропущено", extra={"post_id": ctx.post.id, "reason": reason})
        return advanced(State.IN_REVIEW)

    if ctx.post.review_message_id:
        # Уже отправлено, а состояние закоммитить не успели. Второй раз слать
        # нельзя: владелец получит тот же пост дважды и не поймёт, какой из них
        # настоящий.
        ctx.log.info("пост уже отправлен на ревью", extra={"post_id": ctx.post.id})
        return advanced(State.IN_REVIEW)

    telegram = ctx.project.telegram
    album_id = ctx.post.review_album_message_id

    if ctx.post.review_album_at is None:
        album_id = _send_album(ctx, telegram.chat_id)

    message = ctx.providers.notifier.send_review_text(
        chat_id=telegram.chat_id,
        project=ctx.project.slug,
        title=ctx.post.title or "",
        body=ctx.post.body or "",
        warning=_warning(ctx),
        post_id=ctx.post.id,
        reply_to=album_id,
    )

    with db.write_transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE posts SET review_chat_id = ?, review_message_id = ?, updated_at = ? "
            "WHERE id = ?",
            (message.chat_id, message.message_id, to_iso(now_utc()), ctx.post.id),
        )

    ctx.log.info(
        "пост отправлен на ревью",
        extra={"post_id": ctx.post.id, "chat_id": message.chat_id},
    )
    return advanced(State.IN_REVIEW)


@tracked_call(State.IN_REVIEW)
def await_decision(ctx: StepContext) -> StepResult:
    """Ждём человека. Из этого состояния пост выводит бот, а не тик."""
    if ctx.post.review_message_id:
        # Пост уже лежит у владельца с живыми кнопками. Забрать его по
        # автоодобрению значит опубликовать то, на что человек в этот момент
        # смотрит и, может быть, собирается нажать «В мусор».
        return waiting("жду решения владельца в Telegram")

    if _skips_review(ctx):
        return advanced(State.APPROVED)

    return waiting("жду решения владельца в Telegram")
