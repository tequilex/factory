"""Варианты поста: сделать ещё один, не потеряв предыдущий.

Раньше откат уничтожал то, что было: картинки писались в файлы с одинаковыми
именами и перезаписывались, текст занулялся. Значит, посмотреть второй вариант
можно было только ценой первого — то есть выбирать было нельзя в принципе.
Можно было соглашаться или переделывать вслепую.

Теперь каждый доведённый до ревью вариант сохраняется целиком: текст, промпты,
seed'ы и пути к файлам. Файлы лежат по подпапкам номеров, поэтому новая
генерация не затирает старую. Владелец листает сообщения в переписке и
публикует любой вариант — для этого он восстанавливается в рабочие поля поста.

Почему варианты не листаются стрелками в одном сообщении: Telegram не умеет
менять картинки в медиагруппе. Текст переключился бы, а картинки остались бы от
первого варианта и врали. Поэтому вариант — это своё сообщение со своим
альбомом, а номер в заголовке говорит, какой из скольких перед вами.
"""

from __future__ import annotations

import json
import sqlite3

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.logging import get_logger
from factory.core.models import Post

log = get_logger(__name__)

_ASSET_FIELDS = ("kind", "position", "prompt", "seed", "local_path", "external_ref")


def _assets_of(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, position, prompt, seed, local_path, external_ref "
        "FROM assets WHERE post_id = ? ORDER BY position",
        (post_id,),
    ).fetchall()
    return [{field: row[field] for field in _ASSET_FIELDS} for row in rows]


def count(conn: sqlite3.Connection, post_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM post_versions WHERE post_id = ?", (post_id,)
    ).fetchone()[0]


def record(conn: sqlite3.Connection, post: Post) -> int:
    """Сохранить нынешнее содержимое поста как вариант. Возвращает его номер.

    Повторный вызов для того же номера ничего не дублирует: пост может дойти до
    ревью и вернуться сюда снова, если отправка сорвалась и повторилась.
    """
    stamp = to_iso(now_utc())
    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO post_versions "
            "(post_id, number, title, body, question, factcheck_verdict, "
            " factcheck_notes, assets, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(post_id, number) DO UPDATE SET "
            "title = excluded.title, body = excluded.body, question = excluded.question, "
            "factcheck_verdict = excluded.factcheck_verdict, "
            "factcheck_notes = excluded.factcheck_notes, assets = excluded.assets",
            (
                post.id,
                post.version,
                post.title,
                post.body,
                post.question,
                post.factcheck_verdict,
                post.factcheck_notes,
                json.dumps(_assets_of(conn, post.id), ensure_ascii=False),
                stamp,
            ),
        )
    return post.version


def restore(conn: sqlite3.Connection, post_id: int, number: int) -> bool:
    """Сделать указанный вариант текущим. ``False`` — такого варианта нет.

    Восстанавливается всё, по чему принималось решение: текст, промпты, seed'ы и
    пути к картинкам. Файлы при этом не трогаются — они лежат по подпапкам
    номеров и никуда не девались.
    """
    row = conn.execute(
        "SELECT * FROM post_versions WHERE post_id = ? AND number = ?", (post_id, number)
    ).fetchone()
    if row is None:
        return False

    saved = json.loads(row["assets"] or "[]")
    stamp = to_iso(now_utc())

    with db.write_transaction(conn):
        conn.execute(
            "UPDATE posts SET title = ?, body = ?, question = ?, factcheck_verdict = ?, "
            "factcheck_notes = ?, version = ?, updated_at = ? WHERE id = ?",
            (
                row["title"], row["body"], row["question"], row["factcheck_verdict"],
                row["factcheck_notes"], number, stamp, post_id,
            ),
        )
        for item in saved:
            conn.execute(
                "UPDATE assets SET prompt = ?, seed = ?, local_path = ?, external_ref = ? "
                "WHERE post_id = ? AND kind = ? AND position = ?",
                (
                    item["prompt"], item["seed"], item["local_path"], item["external_ref"],
                    post_id, item["kind"], item["position"],
                ),
            )

    log.info("вариант восстановлен", extra={"post_id": post_id, "f_number": number})
    return True


def next_number(conn: sqlite3.Connection, post_id: int) -> int:
    """Номер для следующей генерации: на единицу больше самого большого."""
    highest = conn.execute(
        "SELECT COALESCE(MAX(number), 0) FROM post_versions WHERE post_id = ?", (post_id,)
    ).fetchone()[0]
    current = conn.execute(
        "SELECT version FROM posts WHERE id = ?", (post_id,)
    ).fetchone()["version"]
    return max(highest, current) + 1
