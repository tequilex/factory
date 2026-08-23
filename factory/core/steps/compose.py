"""images_ready -> composed: assemble the cover locally.

On this stage the step only verifies the pieces are in place. Real composition
with Pillow — red frame, white plate, wrapped Cyrillic headline — lands in
Stage 2 in ``compose/cover.py``.

The rule it will implement is worth stating here, because it is the reason the
step exists at all: text on the image is drawn locally, never by a neural
network. Image models garble Cyrillic.
"""

from __future__ import annotations

from pathlib import Path

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.errors import FactoryError
from factory.core.models import AssetKind, State
from factory.core.retry import tracked_call
from factory.core.steps import StepContext, StepResult, advanced


@tracked_call(State.IMAGES_READY)
def run(ctx: StepContext) -> StepResult:
    row = ctx.conn.execute(
        "SELECT * FROM assets WHERE post_id = ? AND kind = ? ORDER BY position LIMIT 1",
        (ctx.post.id, AssetKind.COVER),
    ).fetchone()

    if row is None:
        raise FactoryError(
            f"У поста {ctx.post.id} нет обложки.",
            why="Шаг генерации промптов не создал запись с kind='cover'.",
            what_to_do=(
                f"Верни пост на генерацию промптов: factory post retry {ctx.post.id}. "
                "См. RUNBOOK.md → «Когда сломалось»."
            ),
        )

    if not row["local_path"] or not Path(row["local_path"]).is_file():
        raise FactoryError(
            f"Файл обложки поста {ctx.post.id} не найден.",
            why=f"Ожидался файл {row['local_path']}.",
            what_to_do=f"Перезапусти пост: factory post retry {ctx.post.id}.",
        )

    if not ctx.post.title:
        raise FactoryError(
            f"У поста {ctx.post.id} нет заголовка для обложки.",
            why="Шаг генерации текста не заполнил поле title.",
            what_to_do=f"Перезапусти пост: factory post retry {ctx.post.id}.",
        )

    with db.write_transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE posts SET updated_at = ? WHERE id = ?", (to_iso(now_utc()), ctx.post.id)
        )

    ctx.log.info("обложка собрана", extra={"post_id": ctx.post.id, "cover": row["local_path"]})
    return advanced(State.COMPOSED)
