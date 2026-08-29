"""Экран «Обзор»: всё ли в порядке, за две секунды и без чтения.

Здесь только выдача. Ни одна цифра не считается заново тем способом, каким её
считает воркер: расписание, дневной лимит и запас тем берутся у тех же функций,
что исполняют правила. Свой подсчёт однажды разойдётся с настоящим, и владелец
увидит «сегодня 1 из 2» там, где система уже отказывается публиковать.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from factory.core import lock, paths, topics
from factory.core.clock import now_utc, to_iso
from factory.core.config import ProjectConfig
from factory.core.models import State
from factory.core.steps.publish import next_slot_start, published_today
from factory.panel import deps

router = APIRouter()


class Health(BaseModel):
    """Жив ли воркер. Панель без него показывает картинку прошлого."""

    tick_age_sec: float | None
    stale: bool
    stale_after_sec: int


class Alert(BaseModel):
    name: str
    scope: str
    raised_at: str


class GroupSummary(BaseModel):
    slug: str
    #: Отдельного названия у группы в конфиге пока нет, поэтому здесь слаг.
    #: TODO: подтвердить у владельца — заводить ли поле `title` в config.yaml.
    title: str
    waiting: int
    approved: int
    working: int
    failed: int
    free_topics: int
    published_today: int
    posts_per_day: int
    spent_today: float
    spent_month: float
    paused: bool
    schedule_off: bool
    next_slot: str | None


class Overview(BaseModel):
    health: Health
    groups: list[GroupSummary]
    alerts: list[Alert]
    #: Проекты, чей конфиг не читается. Молчать о них нельзя: группа просто
    #: исчезла бы с экрана, и владелец решил бы, что её удалили.
    broken: dict[str, str]


def _day_start_utc(project: ProjectConfig, moment: datetime) -> str:
    """Начало сегодняшнего дня группы, в UTC.

    День считается по часовому поясу группы, а не по серверному: публикации
    и лимиты живут в её времени, и расходы должны совпадать с ними.
    """
    local = moment.astimezone(project.vk.tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return to_iso(start.astimezone(moment.tzinfo))


def _spent(conn: sqlite3.Connection, project_id: int, since: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(r.cost_usd), 0) FROM runs r "
        "JOIN posts p ON p.id = r.post_id "
        "WHERE p.project_id = ? AND r.created_at >= ?",
        (project_id, since),
    ).fetchone()
    return round(float(row[0]), 2)


def _counts(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    row = conn.execute(
        "SELECT "
        "  SUM(state = ?) AS waiting, "
        "  SUM(state = ?) AS approved, "
        "  SUM(state = ?) AS failed, "
        "  SUM(state NOT IN (?, ?, ?, ?, ?)) AS working "
        "FROM posts WHERE project_id = ?",
        (
            State.IN_REVIEW, State.APPROVED, State.FAILED,
            # in_review и approved уже посчитаны отдельно: без этого один пост
            # попадал бы сразу в две колонки, и сумма на экране не сходилась бы.
            State.PUBLISHED, State.REJECTED, State.FAILED,
            State.IN_REVIEW, State.APPROVED,
            project_id,
        ),
    ).fetchone()
    return {key: int(row[key] or 0) for key in ("waiting", "approved", "failed", "working")}


def _alerts(conn: sqlite3.Connection) -> list[Alert]:
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'alert:%' ORDER BY value DESC"
    ).fetchall()
    result = []
    for row in rows:
        _, _, rest = row["key"].partition("alert:")
        name, _, scope = rest.partition(":")
        result.append(Alert(name=name, scope=scope, raised_at=row["value"]))
    return result


@router.get("/api/overview", response_model=Overview)
def overview(conn: sqlite3.Connection = Depends(deps.session)) -> Overview:
    moment = now_utc()
    configs = deps.projects()
    month_ago = to_iso(moment - timedelta(days=30))

    groups: list[GroupSummary] = []
    for row in conn.execute("SELECT id, slug FROM projects ORDER BY id").fetchall():
        project = configs.get(row["slug"])
        if project is None:
            # Проект есть в базе, но его конфиг не прочитался. Сводку по нему
            # собрать нечем — он попадёт в broken, и это честнее пустой карточки.
            continue

        counts = _counts(conn, row["id"])
        upcoming = next_slot_start(project, moment)
        groups.append(
            GroupSummary(
                slug=row["slug"],
                title=row["slug"],
                **counts,
                free_topics=topics.counts(conn, row["id"]).free,
                published_today=published_today(conn, project, row["id"]),
                posts_per_day=project.limits.posts_per_day,
                spent_today=_spent(conn, row["id"], _day_start_utc(project, moment)),
                spent_month=_spent(conn, row["id"], month_ago),
                paused=topics.is_paused(conn, row["slug"]),
                schedule_off=topics.schedule_is_off(conn, row["slug"]),
                next_slot=to_iso(upcoming) if upcoming else None,
            )
        )

    return Overview(
        health=Health(
            tick_age_sec=lock.heartbeat_age_sec(conn),
            stale=lock.heartbeat_is_stale(conn),
            stale_after_sec=paths.heartbeat_stale_sec(),
        ),
        groups=groups,
        alerts=_alerts(conn),
        broken=deps.broken_projects(),
    )
