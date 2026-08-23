"""prompts_ready -> images_ready: generate every image that is still missing.

Two rules shape this step:

* an asset that already has a file on disk is skipped — those images were paid
  for, and a restart must not buy them again;
* bytes are written to disk the moment they arrive and never accumulated in a
  list. Four 1080×1350 images held at once is most of the memory budget on a
  1 GB Raspberry Pi.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from factory.core import db, paths
from factory.core.clock import now_utc, to_iso
from factory.core.models import Asset, State
from factory.core.retry import tracked_call
from factory.core.steps import StepContext, StepResult, advanced
from factory.providers.base import IMAGE_HEIGHT, IMAGE_WIDTH


def _pending(conn: sqlite3.Connection, post_id: int) -> list[Asset]:
    rows = conn.execute(
        "SELECT * FROM assets WHERE post_id = ? AND local_path IS NULL ORDER BY position",
        (post_id,),
    ).fetchall()
    return [Asset.from_row(row) for row in rows]


def _still_on_disk(asset: Asset) -> bool:
    return bool(asset.local_path) and Path(asset.local_path).is_file()


def _generate_one(ctx: StepContext, asset: Asset, target_dir: Path) -> tuple[int, str]:
    """Generate one image and write it to disk. Returns the asset id and its path.

    Runs on a worker thread, so it must not touch the database: the SQLite
    connection belongs to the main thread. Recording the path is the caller's job,
    and it happens immediately — see :func:`run`.
    """
    data = ctx.providers.images.generate(
        asset.prompt or "",
        lora=ctx.project.image.lora,
        seed=asset.seed,
        width=IMAGE_WIDTH,
        height=IMAGE_HEIGHT,
    )
    path = target_dir / f"{asset.kind}_{asset.position}.png"
    path.write_bytes(data)
    return asset.id, str(path)


def _record_path(conn, asset_id: int, path: str) -> None:
    """Commit one generated image on its own.

    Deliberately one tiny transaction per image rather than one at the end. If the
    fourth image fails — a timeout, a kill, an out-of-memory — the first three are
    already recorded and the next attempt only generates the remainder. Batching
    the writes would throw away images that have already been paid for, and the
    retry inside ``tracked_call`` would pay for them again on the spot.
    """
    with db.write_transaction(conn):
        conn.execute("UPDATE assets SET local_path = ? WHERE id = ?", (path, asset_id))


@tracked_call(State.PROMPTS_READY)
def run(ctx: StepContext) -> StepResult:
    # Assets whose file vanished (wiped /tmp, moved server) are regenerated.
    with db.write_transaction(ctx.conn):
        for row in ctx.conn.execute(
            "SELECT * FROM assets WHERE post_id = ? AND local_path IS NOT NULL", (ctx.post.id,)
        ).fetchall():
            asset = Asset.from_row(row)
            if not _still_on_disk(asset):
                ctx.log.warning(
                    "файл картинки пропал, будет сгенерирован заново",
                    extra={"post_id": ctx.post.id, "asset_id": asset.id, "path": asset.local_path},
                )
                ctx.conn.execute("UPDATE assets SET local_path = NULL WHERE id = ?", (asset.id,))

    pending = _pending(ctx.conn, ctx.post.id)
    if not pending:
        ctx.log.info("все картинки уже на месте", extra={"post_id": ctx.post.id})
        return advanced(State.IMAGES_READY)

    target_dir = paths.post_tmp_dir(ctx.post.id)
    target_dir.mkdir(parents=True, exist_ok=True)

    workers = max(1, min(paths.max_parallel_images(), len(pending)))
    done = 0

    if workers == 1:
        for asset in pending:
            asset_id, path = _generate_one(ctx, asset, target_dir)
            _record_path(ctx.conn, asset_id, path)
            done += 1
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_generate_one, ctx, asset, target_dir): asset for asset in pending
            }
            # Results are recorded as they arrive, from this thread. A failure in
            # one image then loses only that image, not the whole batch.
            for future in as_completed(futures):
                asset_id, path = future.result()
                _record_path(ctx.conn, asset_id, path)
                done += 1

    with db.write_transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE posts SET updated_at = ? WHERE id = ?", (to_iso(now_utc()), ctx.post.id)
        )

    ctx.log.info(
        "картинки сгенерированы",
        extra={"post_id": ctx.post.id, "count": done, "workers": workers},
    )
    return advanced(State.IMAGES_READY)
