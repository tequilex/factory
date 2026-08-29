"""Картинки поста: подмена своей и перерисовка одной по правленому промпту.

Обе операции — откаты, и на них распространяется правило, которое проект уже
однажды выучил дорого: **каждый откат обязан стирать то, на что смотрит его
шаг.** Шаги пропускают работу при готовых данных, иначе повтор жёг бы деньги, и
забытая отметка означает, что откат не случится вовсе.

Здесь стираются:

* ``review_message_id`` и ``review_album_at`` — иначе пост вернётся на просмотр
  без картинок либо со старым альбомом;
* ``external_ref`` обложки при её замене — на эту отметку смотрит шаг сборки, и
  без её снятия заголовок не будет нарисован на новой картинке;
* ``local_path`` перерисовываемой картинки — на него смотрит шаг генерации.

Приведение размера общее с провайдером: панель и модель обязаны класть на диск
файлы одного формата, иначе сборка обложки растянет чужой кадр.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from random import randint

from PIL import Image

from factory.core import db, paths
from factory.core.clock import now_utc, to_iso
from factory.core.errors import FactoryError
from factory.core.logging import get_logger
from factory.core.models import AssetKind, State
from factory.providers.base import IMAGE_HEIGHT, IMAGE_WIDTH

log = get_logger(__name__)

#: Тот же диапазон, что у шага промптов: seed должен выглядеть одинаково,
#: откуда бы он ни пришёл.
SEED_MAX = 2**31 - 1

#: Больше этого файл не принимаем. Картинка 1080×1350 в PNG весит меньше
#: мегабайта; двадцать — это уже фотография с зеркалки или чей-то архив, и
#: раскодировать его на малине с гигабайтом памяти нечем.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def fit(data: bytes, width: int = IMAGE_WIDTH, height: int = IMAGE_HEIGHT) -> bytes:
    """Привести картинку к нужному размеру, не искажая пропорций.

    Лишнее срезается по центру, и только потом меняется масштаб. Обрезка честнее
    растяжения: кадр становится теснее, но лица и предметы не вытягиваются.

    Живёт здесь, а не в провайдере, потому что нужна двоим: модель отдаёт размер
    с округлением, а владелец приносит что угодно со своего телефона.
    """
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except OSError as exc:
        raise FactoryError(
            "Это не картинка.",
            why=f"Файл не удалось прочитать: {exc}.",
            what_to_do="Подойдёт обычный JPEG или PNG. Проверьте, что файл не битый.",
        ) from exc

    if image.size == (width, height) and image.format == "PNG":
        return data

    image = image.convert("RGB")
    source_width, source_height = image.size
    if source_width * height > source_height * width:
        new_width = round(source_height * width / height)
        offset = (source_width - new_width) // 2
        image = image.crop((offset, 0, offset + new_width, source_height))
    else:
        new_height = round(source_width * height / width)
        offset = (source_height - new_height) // 2
        image = image.crop((0, offset, source_width, offset + new_height))

    image = image.resize((width, height), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _asset(conn: sqlite3.Connection, post_id: int, position: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM assets WHERE post_id = ? AND position = ?", (post_id, position)
    ).fetchone()


def _target_state(kind: str) -> str:
    """Куда вернуть пост.

    Обложку надо собрать заново — на ней печатается заголовок. Сопровождающей
    картинке сборка не нужна, ей достаточно новой отправки альбома.
    """
    return State.IMAGES_READY if kind == AssetKind.COVER else State.COMPOSED


def replace(conn: sqlite3.Connection, post_id: int, position: int, data: bytes) -> bool:
    """Поставить свою картинку вместо сгенерированной. ``False`` — нельзя.

    Нельзя означает: поста нет, картинки с таким номером нет, или пост уже не
    ждёт решения. Последнее важно так же, как у любого другого отката: пост мог
    уехать на переделку, и подмена файла легла бы поверх чужого варианта.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise FactoryError(
            "Файл слишком большой.",
            why=f"Прислано {len(data) // 1024 // 1024} МБ при пределе "
            f"{MAX_UPLOAD_BYTES // 1024 // 1024} МБ.",
            what_to_do="Уменьшите картинку или пришлите другую.",
        )

    prepared = fit(data)

    with db.write_transaction(conn):
        post = conn.execute(
            "SELECT id, version FROM posts WHERE id = ? AND state = ?",
            (post_id, State.IN_REVIEW),
        ).fetchone()
        if post is None:
            return False

        asset = _asset(conn, post_id, position)
        if asset is None:
            return False

        target_dir = paths.post_tmp_dir(post_id, post["version"])
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{asset['kind']}_{position}.png"
        path.write_bytes(prepared)

        conn.execute(
            "UPDATE assets SET local_path = ?, replaced_by_owner = 1, "
            # Обложку надо собрать заново: заголовок печатается поверх картинки,
            # а отметка о сборке — это то, на что смотрит шаг.
            "external_ref = CASE WHEN kind = ? THEN NULL ELSE external_ref END "
            "WHERE id = ?",
            (str(path), AssetKind.COVER, asset["id"]),
        )
        _back_to_review(conn, post_id, _target_state(asset["kind"]))

    log.info(
        "картинка заменена владельцем",
        extra={"post_id": post_id, "position": position, "bytes": len(prepared)},
    )
    return True


def redraw(
    conn: sqlite3.Connection, post_id: int, position: int, prompt: str | None = None
) -> bool:
    """Перерисовать одну картинку. ``False`` — нельзя.

    Сам вызов модели делает воркер: панель только снимает файл и ставит новый
    seed. Иначе экран ждал бы ответа провайдера, а деньги списывались бы в
    обход учёта расходов, который живёт в шаге.
    """
    with db.write_transaction(conn):
        post = conn.execute(
            "SELECT id FROM posts WHERE id = ? AND state = ?", (post_id, State.IN_REVIEW)
        ).fetchone()
        if post is None:
            return False

        asset = _asset(conn, post_id, position)
        if asset is None:
            return False

        conn.execute(
            "UPDATE assets SET local_path = NULL, replaced_by_owner = 0, seed = ?, "
            "prompt = COALESCE(?, prompt), "
            "external_ref = CASE WHEN kind = ? THEN NULL ELSE external_ref END "
            "WHERE id = ?",
            (randint(1, SEED_MAX), prompt, AssetKind.COVER, asset["id"]),
        )
        # Шаг генерации рисует только то, у чего нет файла, — значит остальные
        # три картинки останутся прежними и денег не потребуют.
        _back_to_review(conn, post_id, State.PROMPTS_READY)

    log.info(
        "картинка отправлена на перерисовку",
        extra={"post_id": post_id, "position": position, "new_prompt": bool(prompt)},
    )
    return True


def _back_to_review(conn: sqlite3.Connection, post_id: int, state: str) -> None:
    """Вернуть пост в цепочку и снять всё, что помешает ему прийти заново.

    Отметки об альбоме снимаются всегда: картинки изменились, значит показывать
    старый альбом нельзя. Без этого пост возвращается на просмотр с прежними
    картинками и текстом «вот новые» — то есть врёт.
    """
    conn.execute(
        "UPDATE posts SET state = ?, retry_count = 0, last_error = NULL, "
        "next_attempt_at = NULL, review_message_id = NULL, review_album_at = NULL, "
        "review_album_message_id = NULL, updated_at = ? WHERE id = ?",
        (state, to_iso(now_utc()), post_id),
    )


def missing_files(conn: sqlite3.Connection, post_id: int) -> list[int]:
    """Позиции картинок, чьи файлы пропали с диска.

    Нужна панели: показывать «готово» по записи в базе, когда файла нет, значит
    обещать картинку, которой не будет.
    """
    rows = conn.execute(
        "SELECT position, local_path FROM assets WHERE post_id = ?", (post_id,)
    ).fetchall()
    return [
        row["position"]
        for row in rows
        if row["local_path"] and not Path(row["local_path"]).is_file()
    ]
