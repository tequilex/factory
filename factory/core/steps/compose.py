"""images_ready -> composed: assemble the cover on top of the generated image.

The cover file is overwritten in place: the raw generated picture has no value of
its own once the headline is on it, and keeping both would double the disk use of
every post for nothing.

Overwriting means the step must know whether it has already run — re-composing
would draw the headline over a headline. That is what ``assets.external_ref``
records here.
"""

from __future__ import annotations

from pathlib import Path

from factory.compose import cover
from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.errors import FactoryError
from factory.core.models import Asset, AssetKind, State
from factory.core.retry import tracked_call
from factory.core.steps import StepContext, StepResult, advanced

# Записывается в assets.external_ref после сборки. Отличает готовую обложку от
# исходной картинки, поверх которой ещё ничего не нарисовано.
COMPOSED_MARK = "composed"


def _cover_asset(ctx: StepContext) -> Asset:
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
    return Asset.from_row(row)


@tracked_call(State.IMAGES_READY)
def run(ctx: StepContext) -> StepResult:
    asset = _cover_asset(ctx)

    if asset.external_ref == COMPOSED_MARK:
        ctx.log.info("обложка уже собрана", extra={"post_id": ctx.post.id})
        return advanced(State.COMPOSED)

    if not asset.local_path or not Path(asset.local_path).is_file():
        raise FactoryError(
            f"Файл обложки поста {ctx.post.id} не найден.",
            why=f"Ожидался файл {asset.local_path}.",
            what_to_do=f"Перезапусти пост: factory post retry {ctx.post.id}.",
        )

    if not ctx.post.title:
        raise FactoryError(
            f"У поста {ctx.post.id} нет заголовка для обложки.",
            why="Шаг генерации текста не заполнил поле title.",
            what_to_do=f"Перезапусти пост: factory post retry {ctx.post.id}.",
        )

    path = Path(asset.local_path)
    composed = cover.render(
        path.read_bytes(), ctx.post.title, ctx.project.cover_template_path
    )
    path.write_bytes(composed)

    with db.write_transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE assets SET external_ref = ? WHERE id = ?", (COMPOSED_MARK, asset.id)
        )
        ctx.conn.execute(
            "UPDATE posts SET updated_at = ? WHERE id = ?", (to_iso(now_utc()), ctx.post.id)
        )

    ctx.log.info(
        "обложка собрана",
        extra={"post_id": ctx.post.id, "cover": str(path), "bytes": len(composed)},
    )
    return advanced(State.COMPOSED)
