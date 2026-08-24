"""queued -> text_ready: ask the LLM for the post itself."""

from __future__ import annotations

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.models import State
from factory.core.retry import tracked_call
from factory.core.steps import StepContext, StepResult, advanced
from factory.providers.base import PostDraft

SYSTEM_SUFFIX = (
    "Верни ответ строго по заданной схеме: короткий заголовок для обложки, "
    "текст поста и вопрос-хук в конце."
)


def build_prompt(ctx: StepContext) -> tuple[str, str]:
    """Assemble the system and user prompts.

    Only text meant for the model goes in. Anything explaining things to a human
    belongs in the project README — the model reads whatever it is given as part
    of the task.
    """
    project = ctx.project
    parts = [project.voice()]

    examples = project.style_examples()
    if examples:
        parts.append("Примеры постов в нужном стиле:\n\n" + "\n\n---\n\n".join(examples))

    parts.append(SYSTEM_SUFFIX)
    system = "\n\n".join(parts)

    topic = ctx.conn.execute(
        "SELECT title, angle FROM topics WHERE id = ?", (ctx.post.topic_id,)
    ).fetchone()

    low, high = project.content.target_length
    user_parts = [f"Тема: {topic['title']}"]
    if topic["angle"]:
        user_parts.append(f"Угол подачи: {topic['angle']}")
    user_parts.append(f"Структура: {', '.join(project.content.post_structure)}")
    user_parts.append(f"Объём: {low}-{high} символов")

    return system, "\n".join(user_parts)


@tracked_call(State.QUEUED)
def run(ctx: StepContext) -> StepResult:
    if ctx.post.title and ctx.post.body:
        # Already written; a repeat run must not pay for the same text twice.
        ctx.log.info("текст уже есть, шаг пропущен", extra={"post_id": ctx.post.id})
        return advanced(State.TEXT_READY)

    system, user = build_prompt(ctx)
    draft = ctx.charge(ctx.providers.llm.complete(system, user, schema=PostDraft))

    with db.write_transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE posts SET title = ?, body = ?, question = ?, updated_at = ? WHERE id = ?",
            (draft.title, draft.body, draft.question, to_iso(now_utc()), ctx.post.id),
        )

    return advanced(State.TEXT_READY)
