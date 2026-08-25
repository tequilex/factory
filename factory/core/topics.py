"""Темы: добавить, посчитать, показать.

Вынесено из командной строки, потому что тем же самым занимается бот. Владелец
работает с телефона, а тревога «скоро публиковать нечего» до этого советовала
выполнить команду в терминале — то есть предлагала сделать невозможное.

Правила добавления везде одни: пустые строки и повторы пропускаются молча,
порядок сохраняется. Дубли отсекаются по точному совпадению заголовка — это
грубо, но предсказуемо, а «похожие» темы владелец различает лучше кода.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from factory.core import db
from factory.core.logging import get_logger
from factory.core.models import TopicStatus

log = get_logger(__name__)

#: Сколько тем показывать в списке. Больше не влезет в одно сообщение, а
#: владельцу нужен не полный перечень, а понимание, чем система занята.
PREVIEW = 10


@dataclass(frozen=True)
class Added:
    added: int
    skipped: int


@dataclass(frozen=True)
class Counts:
    free: int
    taken: int
    used: int

    @property
    def total(self) -> int:
        return self.free + self.taken + self.used


def add(conn: sqlite3.Connection, project_id: int, lines: list[str]) -> Added:
    """Добавить темы. Возвращает, сколько принято и сколько пропущено."""
    existing = {
        row["title"]
        for row in conn.execute(
            "SELECT title FROM topics WHERE project_id = ?", (project_id,)
        ).fetchall()
    }

    added = skipped = 0
    with db.write_transaction(conn):
        for line in lines:
            title = line.strip()
            if not title or title in existing:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO topics (project_id, title, status) VALUES (?, ?, ?)",
                (project_id, title, TopicStatus.FREE),
            )
            existing.add(title)
            added += 1

    if added:
        log.info("темы добавлены", extra={"project_id": project_id, "count": added})
    return Added(added=added, skipped=skipped)


def counts(conn: sqlite3.Connection, project_id: int) -> Counts:
    rows = dict(
        conn.execute(
            "SELECT status, COUNT(*) FROM topics WHERE project_id = ? GROUP BY status",
            (project_id,),
        ).fetchall()
    )
    return Counts(
        free=rows.get(TopicStatus.FREE, 0),
        taken=rows.get(TopicStatus.TAKEN, 0),
        used=rows.get(TopicStatus.USED, 0),
    )


def upcoming(conn: sqlite3.Connection, project_id: int, limit: int = PREVIEW) -> list[str]:
    """Ближайшие свободные темы — в том порядке, в котором их возьмут."""
    rows = conn.execute(
        "SELECT title FROM topics WHERE project_id = ? AND status = ? ORDER BY id LIMIT ?",
        (project_id, TopicStatus.FREE, limit),
    ).fetchall()
    return [row["title"] for row in rows]


def in_progress(conn: sqlite3.Connection, project_id: int, limit: int = PREVIEW) -> list[str]:
    """Темы, по которым уже пишется пост."""
    rows = conn.execute(
        "SELECT title FROM topics WHERE project_id = ? AND status = ? ORDER BY id LIMIT ?",
        (project_id, TopicStatus.TAKEN, limit),
    ).fetchall()
    return [row["title"] for row in rows]


def set_paused(conn: sqlite3.Connection, slug: str, paused: bool) -> bool:
    """Поставить проект на паузу или снять. ``False`` — такого проекта нет.

    Пауза останавливает проект целиком: ни новых постов, ни публикаций. Тик
    просто перестаёт его видеть.

    TODO: подтвердить у владельца. Возможен и мягкий вариант — готовить посты,
    но не публиковать. Выбран жёсткий: «пауза» на время отъезда означает
    «ничего не делай», а посты, накопленные за неделю, придётся разбирать пачкой,
    и половина из них к тому времени устареет.
    """
    with db.write_transaction(conn):
        cursor = conn.execute(
            "UPDATE projects SET is_active = ? WHERE slug = ?", (0 if paused else 1, slug)
        )
    if cursor.rowcount:
        log.info("проект переключён", extra={"slug": slug, "f_paused": paused})
    return bool(cursor.rowcount)


def is_paused(conn: sqlite3.Connection, slug: str) -> bool:
    row = conn.execute("SELECT is_active FROM projects WHERE slug = ?", (slug,)).fetchone()
    return row is not None and not row["is_active"]
