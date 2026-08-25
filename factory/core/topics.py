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


#: Состояние поста человеческим языком. Владелец не обязан знать названия
#: состояний машины, ему нужно понимать, чего ждать.
STATE_WORDS: dict[str, str] = {
    "queued": "пишется текст",
    "text_ready": "проверяются факты",
    "factchecked": "придумываются сцены",
    "prompts_ready": "рисуются картинки",
    "images_ready": "собирается обложка",
    "composed": "отправляется вам",
    "in_review": "ждёт вашего решения",
    "approved": "одобрен, ждёт слота",
    "failed": "сломался",
}


@dataclass(frozen=True)
class TopicLine:
    """Тема и что с ней происходит."""

    title: str
    note: str
    url: str | None = None


def upcoming(conn: sqlite3.Connection, project_id: int, limit: int = PREVIEW) -> list[str]:
    """Ближайшие свободные темы — в том порядке, в котором их возьмут."""
    rows = conn.execute(
        "SELECT title FROM topics WHERE project_id = ? AND status = ? ORDER BY id LIMIT ?",
        (project_id, TopicStatus.FREE, limit),
    ).fetchall()
    return [row["title"] for row in rows]


def in_progress(conn: sqlite3.Connection, project_id: int, limit: int = PREVIEW) -> list[TopicLine]:
    """Темы, по которым пишется пост, — с тем, на каком он шаге.

    Без шага список бесполезен: «в работе» одинаково выглядит и у поста,
    который ждёт решения владельца, и у того, который сломался час назад.
    """
    rows = conn.execute(
        "SELECT t.title, p.state FROM topics t "
        "LEFT JOIN posts p ON p.topic_id = t.id AND p.state NOT IN ('published', 'rejected') "
        "WHERE t.project_id = ? AND t.status = ? ORDER BY t.id LIMIT ?",
        (project_id, TopicStatus.TAKEN, limit),
    ).fetchall()
    return [
        TopicLine(
            title=row["title"],
            note=STATE_WORDS.get(row["state"] or "", "готовится"),
        )
        for row in rows
    ]


def done(conn: sqlite3.Connection, project_id: int, limit: int = PREVIEW) -> list[TopicLine]:
    """Отработанные темы — со ссылкой на вышедший пост.

    Свежие сверху: «что сделано» спрашивают про последнее, а не про первое.
    """
    rows = conn.execute(
        "SELECT t.title, p.external_id FROM topics t "
        "LEFT JOIN posts p ON p.topic_id = t.id AND p.state = 'published' "
        "WHERE t.project_id = ? AND t.status = ? "
        "ORDER BY COALESCE(t.used_at, '') DESC, t.id DESC LIMIT ?",
        (project_id, TopicStatus.USED, limit),
    ).fetchall()

    lines = []
    for row in rows:
        external = row["external_id"]
        lines.append(
            TopicLine(
                title=row["title"],
                note="опубликован" if external else "закрыта без поста",
                url=f"https://vk.com/wall{external}" if external else None,
            )
        )
    return lines


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


#: Ключ в ``meta``: этот проект публикует в обход расписания.
#:
#: Настройка живёт в базе, а не в переменной окружения, потому что переключать
#: её владелец должен из телефона. Переменная ``FACTORY_IGNORE_SCHEDULE``
#: остаётся глобальным рубильником для отладки и главнее: она задаётся тем, кто
#: запускает процесс, и молча отменять её решение нельзя.
_SCHEDULE_OFF = "schedule_off:"


def schedule_is_off(conn: sqlite3.Connection, slug: str) -> bool:
    """Публикует ли проект в обход расписания."""
    from factory.core import paths

    if paths.ignore_schedule():
        return True
    row = conn.execute(
        "SELECT 1 FROM meta WHERE key = ?", (f"{_SCHEDULE_OFF}{slug}",)
    ).fetchone()
    return row is not None


def set_schedule_off(conn: sqlite3.Connection, slug: str, off: bool) -> None:
    """Включить или выключить расписание для проекта."""
    from factory.core.clock import now_utc, to_iso

    key = f"{_SCHEDULE_OFF}{slug}"
    with db.write_transaction(conn):
        if off:
            stamp = to_iso(now_utc())
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, '1', ?) "
                "ON CONFLICT(key) DO UPDATE SET updated_at = excluded.updated_at",
                (key, stamp),
            )
        else:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    log.info("расписание переключено", extra={"slug": slug, "f_off": off})
