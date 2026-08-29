"""Экраны «Посты» и «Просмотр поста».

Отдельного внимания стоит отдача файлов картинок. Путь к файлу приходит **из
базы**, а не из запроса, и перед отправкой проверяется, что он действительно
лежит в хранилище фабрики. Иначе адрес вида ``../../data/.env`` выдал бы наружу
файл секретов — панель работает от того же пользователя, что и воркер, и
прочитать может всё то же самое.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from factory.core import paths, versions
from factory.core.models import State
from factory.panel import deps

router = APIRouter()

#: Сколько постов отдавать списком за раз. Панель работает на одноплатнике,
#: и «показать всё» однажды означает выгрузить в память годовую историю.
PAGE = 50


class Asset(BaseModel):
    position: int
    kind: str
    prompt: str | None
    seed: int | None
    #: Готова ли картинка. Пути наружу не отдаются: файл забирается отдельным
    #: адресом по номеру поста и позиции.
    #: Файл ЕСТЬ НА ДИСКЕ, а не «в базе записан путь». Это разные вещи: после
    #: публикации папка поста вычищается, а строки остаются. По записи в базе
    #: панель показывала бы битые картинки — их пыталась загрузить и не могла.
    ready: bool
    composed: bool
    #: Эту картинку поставил владелец, а не модель. Показывается ярлыком: иначе
    #: «перерисовать все» однажды сотрёт то, что он подобрал сам.
    replaced_by_owner: bool


class PostBrief(BaseModel):
    id: int
    project: str
    state: str
    state_label: str
    title: str | None
    cost: float
    created_at: str
    updated_at: str
    external_id: str | None
    last_error: str | None
    #: Чего пост ждёт прямо сейчас. Подпись состояния этого не говорит: «рисуются
    #: картинки» выглядит как работа, даже когда рисовать нечем.
    waiting_reason: str | None
    has_cover: bool


class PostDetail(PostBrief):
    body: str | None
    question: str | None
    factcheck_verdict: str | None
    factcheck_notes: str | None
    version: int
    versions_total: int
    retry_count: int
    scheduled_at: str | None
    published_at: str | None
    assets: list[Asset]


def _cost(conn: sqlite3.Connection, post_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE post_id = ?", (post_id,)
    ).fetchone()
    return round(float(row[0]), 2)


def _on_disk(path: str | None) -> bool:
    """Есть ли файл. Запись в базе этого не гарантирует.

    Папка поста вычищается после публикации, а строки в ``assets`` остаются —
    иначе пропала бы история промптов и seed'ов.
    """
    return bool(path) and Path(path).is_file()


def _brief(conn: sqlite3.Connection, row: sqlite3.Row) -> PostBrief:
    cover = conn.execute(
        "SELECT local_path FROM assets WHERE post_id = ? AND kind = 'cover'", (row["id"],)
    ).fetchone()
    return PostBrief(
        id=row["id"],
        project=row["slug"],
        state=row["state"],
        state_label=deps.label_of(row["state"]),
        title=row["title"],
        cost=_cost(conn, row["id"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        external_id=row["external_id"],
        last_error=row["last_error"],
        waiting_reason=row["waiting_reason"],
        has_cover=_on_disk(cover["local_path"]) if cover else False,
    )


@router.get("/api/posts", response_model=list[PostBrief])
def list_posts(
    conn: sqlite3.Connection = Depends(deps.session),
    project: str | None = Query(default=None),
    state: str | None = Query(default=None),
    limit: int = Query(default=PAGE, ge=1, le=200),
) -> list[PostBrief]:
    where = ["1 = 1"]
    params: list = []
    if project:
        where.append("p.slug = ?")
        params.append(project)
    if state:
        where.append("o.state = ?")
        params.append(state)
    params.append(limit)

    rows = conn.execute(
        "SELECT o.*, p.slug FROM posts o JOIN projects p ON p.id = o.project_id "
        f"WHERE {' AND '.join(where)} ORDER BY o.id DESC LIMIT ?",
        params,
    ).fetchall()
    return [_brief(conn, row) for row in rows]


@router.get("/api/posts/{post_id}", response_model=PostDetail)
def post_detail(
    post_id: int, conn: sqlite3.Connection = Depends(deps.session)
) -> PostDetail:
    row = conn.execute(
        "SELECT o.*, p.slug FROM posts o JOIN projects p ON p.id = o.project_id WHERE o.id = ?",
        (post_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Такого поста нет.")

    assets = [
        Asset(
            position=item["position"],
            kind=item["kind"],
            prompt=item["prompt"],
            seed=item["seed"],
            ready=_on_disk(item["local_path"]),
            composed=item["external_ref"] == "composed",
            replaced_by_owner=bool(item["replaced_by_owner"]),
        )
        for item in conn.execute(
            "SELECT * FROM assets WHERE post_id = ? "
            "ORDER BY CASE kind WHEN 'cover' THEN 0 ELSE 1 END, position",
            (post_id,),
        ).fetchall()
    ]

    brief = _brief(conn, row)
    return PostDetail(
        **brief.model_dump(),
        body=row["body"],
        question=row["question"],
        factcheck_verdict=row["factcheck_verdict"],
        factcheck_notes=row["factcheck_notes"],
        version=row["version"],
        # Вариантов всегда хотя бы один — текущий. В post_versions лежат только
        # доведённые до ревью, поэтому без этого «вариант 1 из 0» на свежем посте.
        versions_total=max(versions.count(conn, post_id), row["version"]),
        retry_count=row["retry_count"],
        scheduled_at=row["scheduled_at"],
        published_at=row["published_at"],
        assets=assets,
    )


@router.get("/api/posts/{post_id}/image/{position}")
def post_image(
    post_id: int, position: int, conn: sqlite3.Connection = Depends(deps.session)
) -> FileResponse:
    """Файл картинки. Путь берётся из базы и проверяется на принадлежность."""
    row = conn.execute(
        "SELECT local_path FROM assets WHERE post_id = ? AND position = ?",
        (post_id, position),
    ).fetchone()
    if row is None or not row["local_path"]:
        raise HTTPException(status_code=404, detail="Картинки пока нет.")

    path = Path(row["local_path"]).resolve()
    root = paths.tmp_dir().resolve()
    # Проверка не паранойя: в базу путь попадает из кода, но панель читает
    # файлы правами воркера, и одна испорченная строка не должна открывать
    # наружу файл секретов.
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл картинки не найден.")

    return FileResponse(path, media_type="image/png")


@router.get("/api/states", response_model=dict[str, str])
def states() -> dict[str, str]:
    """Все состояния и их подписи — чтобы фронт не хранил свой список.

    Список, продублированный на фронте, разъедется с системой при первом же
    новом состоянии, и владелец увидит пустое место вместо подписи.
    """
    return {state.value: deps.label_of(state.value) for state in State}
