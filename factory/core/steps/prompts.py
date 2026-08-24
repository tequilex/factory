"""factchecked -> prompts_ready: turn the finished text into scene prompts.

Rows are created here but left without ``local_path``; the ``images`` step fills
those in. Splitting it that way is what lets a crash between the two steps resume
without regenerating — and without paying for — images that already exist.
"""

from __future__ import annotations

import random

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.models import AssetKind, State
from factory.core.retry import tracked_call
from factory.core.steps import StepContext, StepResult, advanced
from factory.providers.base import ScenePrompts

SYSTEM = (
    "You write prompts for an image generation model. Read the post and propose "
    "scenes featuring the same recurring character. Answer in English, strictly "
    "following the given schema. One expressive close-up scene for the cover, "
    "and the requested number of supporting scenes."
)

SEED_MAX = 2**31 - 1


def _with_style(prompt: str, style: str) -> str:
    return f"{prompt}, {style}" if style else prompt


@tracked_call(State.FACTCHECKED)
def run(ctx: StepContext) -> StepResult:
    existing = ctx.conn.execute(
        "SELECT COUNT(*) FROM assets WHERE post_id = ?", (ctx.post.id,)
    ).fetchone()[0]
    if existing:
        ctx.log.info("промпты уже созданы", extra={"post_id": ctx.post.id})
        return advanced(State.PROMPTS_READY)

    inline_count = ctx.project.image.inline_count
    user = (
        f"Post text:\n{ctx.post.body}\n\n"
        f"Give one cover scene and exactly {inline_count} inline scenes."
    )
    scenes = ctx.charge(ctx.providers.llm.complete(SYSTEM, user, schema=ScenePrompts))

    style = ctx.project.image.scene_style
    stamp = to_iso(now_utc())
    rows = [
        (ctx.post.id, AssetKind.COVER, 0, _with_style(scenes.cover, style), random.randint(1, SEED_MAX), stamp)
    ]
    for position, scene in enumerate(scenes.inline[:inline_count], start=1):
        rows.append(
            (ctx.post.id, AssetKind.INLINE, position, _with_style(scene, style), random.randint(1, SEED_MAX), stamp)
        )

    with db.write_transaction(ctx.conn):
        ctx.conn.executemany(
            "INSERT INTO assets (post_id, kind, position, prompt, seed, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    ctx.log.info("промпты сцен готовы", extra={"post_id": ctx.post.id, "count": len(rows)})
    return advanced(State.PROMPTS_READY)
