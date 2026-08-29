"""Темы, расходы и лента событий.

Три экрана в одном модуле: у всех трёх одна природа — списки из базы без единого
решения. Разносить их по файлам ради симметрии значило бы плодить модули по
двадцать строк.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from factory.core import topics
from factory.core.clock import now_utc, to_iso
from factory.panel import deps

router = APIRouter()

#: Сколько строк отдавать в списках. Панель на одноплатнике: «показать всё»
#: однажды означает выгрузить в память годовую историю.
PAGE = 100

#: Понятные названия шагов для ленты и разбивки расходов. Шаг в базе назван по
#: состоянию, ИЗ которого он выполнялся, — читать это владельцу незачем.
STEP_WORDS: dict[str, str] = {
    "queued": "написан текст",
    "text_ready": "проверены факты",
    "factchecked": "придуманы сцены",
    "prompts_ready": "нарисованы картинки",
    "images_ready": "собрана обложка",
    "composed": "отправлен на просмотр",
    "in_review": "ждёт решения",
    "approved": "опубликован",
}


class TopicLine(BaseModel):
    id: int
    title: str
    note: str | None = None
    #: Ссылка на вышедший пост, если тема уже отработана.
    url: str | None = None


class TopicsView(BaseModel):
    free: int
    taken: int
    used: int
    #: На сколько дней хватит запаса при текущей скорости выпуска.
    days_left: float | None
    upcoming: list[TopicLine]
    in_progress: list[TopicLine]
    done: list[TopicLine]


class SpendingDay(BaseModel):
    day: str
    text: float
    factcheck: float
    images: float
    other: float

    @property
    def total(self) -> float:
        return self.text + self.factcheck + self.images + self.other


class Spending(BaseModel):
    days: list[SpendingDay]
    total: float
    posts: int
    #: Средняя цена поста. Считается по постам, у которых расходы вообще были:
    #: посты без единого платного вызова занизили бы её и успокоили зря.
    average_post: float | None


class Event(BaseModel):
    at: str
    post_id: int | None
    project: str | None
    step: str
    step_label: str
    ok: bool
    cost: float | None
    error: str | None


def _project_id(conn: sqlite3.Connection, slug: str) -> int:
    row = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Проект «{slug}» не подключён.")
    return int(row["id"])


@router.get("/api/topics/{slug}", response_model=TopicsView)
def topics_view(
    slug: str,
    conn: sqlite3.Connection = Depends(deps.session),
    limit: int = Query(default=PAGE, ge=1, le=500),
) -> TopicsView:
    project_id = _project_id(conn, slug)
    counts = topics.counts(conn, project_id)
    configs = deps.projects()
    per_day = configs[slug].limits.posts_per_day if slug in configs else None

    rows = conn.execute(
        "SELECT id, title FROM topics WHERE project_id = ? AND status = 'free' "
        f"ORDER BY {topics.QUEUE_ORDER} LIMIT ?",
        (project_id, limit),
    ).fetchall()

    return TopicsView(
        free=counts.free,
        taken=counts.taken,
        used=counts.used,
        days_left=round(counts.free / per_day, 1) if per_day else None,
        upcoming=[TopicLine(id=row["id"], title=row["title"]) for row in rows],
        in_progress=[
            TopicLine(id=0, title=line.title, note=line.note)
            for line in topics.in_progress(conn, project_id, limit=limit)
        ],
        done=[
            TopicLine(id=0, title=line.title, note=line.note, url=line.url)
            for line in topics.done(conn, project_id, limit=limit)
        ],
    )


@router.get("/api/spending", response_model=Spending)
def spending(
    conn: sqlite3.Connection = Depends(deps.session),
    project: str | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
) -> Spending:
    since = to_iso(now_utc() - timedelta(days=days))
    where = ["r.created_at >= ?", "r.cost_usd IS NOT NULL"]
    params: list = [since]
    if project:
        where.append("p.slug = ?")
        params.append(project)

    rows = conn.execute(
        "SELECT substr(r.created_at, 1, 10) AS day, r.step, SUM(r.cost_usd) AS spent "
        "FROM runs r JOIN posts o ON o.id = r.post_id "
        "JOIN projects p ON p.id = o.project_id "
        f"WHERE {' AND '.join(where)} GROUP BY day, r.step ORDER BY day",
        params,
    ).fetchall()

    by_day: dict[str, SpendingDay] = {}
    for row in rows:
        day = by_day.setdefault(
            row["day"], SpendingDay(day=row["day"], text=0, factcheck=0, images=0, other=0)
        )
        spent = round(float(row["spent"]), 4)
        if row["step"] == "queued":
            day.text += spent
        elif row["step"] == "text_ready":
            day.factcheck += spent
        elif row["step"] == "prompts_ready":
            day.images += spent
        else:
            # Промпты сцен и всё, что появится позже. Отдельной полосой их не
            # показываем — на графике это доли процента.
            day.other += spent

    total = round(sum(day.total for day in by_day.values()), 2)
    counted = conn.execute(
        "SELECT COUNT(DISTINCT r.post_id) FROM runs r JOIN posts o ON o.id = r.post_id "
        "JOIN projects p ON p.id = o.project_id "
        f"WHERE {' AND '.join(where)}",
        params,
    ).fetchone()[0]

    return Spending(
        days=[by_day[key] for key in sorted(by_day)],
        total=total,
        posts=int(counted),
        average_post=round(total / counted, 2) if counted else None,
    )


@router.get("/api/events", response_model=list[Event])
def events(
    conn: sqlite3.Connection = Depends(deps.session),
    project: str | None = Query(default=None),
    only_errors: bool = Query(default=False),
    limit: int = Query(default=PAGE, ge=1, le=500),
) -> list[Event]:
    where = ["1 = 1"]
    params: list = []
    if project:
        where.append("p.slug = ?")
        params.append(project)
    if only_errors:
        where.append("r.ok = 0")
    params.append(limit)

    rows = conn.execute(
        "SELECT r.created_at, r.post_id, r.step, r.ok, r.cost_usd, r.error, p.slug "
        "FROM runs r LEFT JOIN posts o ON o.id = r.post_id "
        "LEFT JOIN projects p ON p.id = o.project_id "
        f"WHERE {' AND '.join(where)} ORDER BY r.id DESC LIMIT ?",
        params,
    ).fetchall()

    return [
        Event(
            at=row["created_at"],
            post_id=row["post_id"],
            project=row["slug"],
            step=row["step"],
            step_label=STEP_WORDS.get(row["step"], row["step"]),
            ok=bool(row["ok"]),
            cost=round(float(row["cost_usd"]), 4) if row["cost_usd"] is not None else None,
            # Ошибка отдаётся целиком: она уже написана человеческим языком в
            # трёх частях, и обрезать её значит потерять «что делать».
            error=row["error"],
        )
        for row in rows
    ]
