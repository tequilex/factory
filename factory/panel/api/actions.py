"""Действия над постом.

Тонкая обёртка и ничего больше. Каждое решение — вызов ``core/decisions.apply()``,
той же функции, которой пользуется бот. Своей логики решений здесь быть не
должно: два интерфейса к одной логике неизбежно разойдутся в поведении, и
разойдутся они молча.

Отказ тоже общий по смыслу. Пост мог уехать дальше, пока владелец смотрел на
экран с другого устройства, и в ответ на устаревшее нажатие надо показать не
ошибку, а то, что с постом на самом деле. «Решение уже принято» на посте,
который просто переделывается, владельца однажды уже сбило с толку.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from factory.core import db, decisions, edits, versions
from factory.core.decisions import Decision
from factory.core.models import State
from factory.panel import deps

router = APIRouter()

#: Что владелец увидит, если нажал по устаревшему экрану. Ключ — состояние, в
#: котором пост оказался на самом деле.
STALE_REASON: dict[str, str] = {
    State.IN_REVIEW: "Пост снова ждёт решения.",
    State.APPROVED: "Пост уже одобрен и ждёт публикации.",
    State.PUBLISHED: "Пост уже вышел в группу — отменить нельзя. Удалять надо в самой группе.",
    State.REJECTED: "Пост выброшен, тема вернулась в очередь.",
    State.FAILED: "Пост сломался. Его можно попробовать починить.",
}


class DecisionRequest(BaseModel):
    decision: Decision
    #: Вариант, под которым нажали. Одобряется именно он, а не последний
    #: сделанный: без этого «Опубликовать» уходило не тем постом.
    version: int | None = None


class EditRequest(BaseModel):
    title: str | None = None
    body: str = Field(min_length=1)


class Applied(BaseModel):
    ok: bool
    state: str
    state_label: str
    #: Что именно произойдёт дальше. Пустая строка означала бы «готово», а
    #: готово здесь никогда не бывает: выполняет воркер, не панель.
    what_next: str


def _post(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT o.*, p.slug FROM posts o JOIN projects p ON p.id = o.project_id WHERE o.id = ?",
        (post_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Такого поста нет.")
    return row


def _refuse(conn: sqlite3.Connection, post_id: int) -> None:
    """Отказ, объясняющий состояние поста, а не общее «нельзя»."""
    row = _post(conn, post_id)
    raise HTTPException(
        status_code=409,
        detail=STALE_REASON.get(row["state"], "Пост сейчас переделывается, решать пока нечего."),
    )


#: Что произойдёт после каждого решения. Про сроки публикации здесь намеренно
#: молчим: слот считает шаг публикации, и вторая арифметика в панели однажды
#: пообещает время, которого не будет.
WHAT_NEXT: dict[Decision, str] = {
    Decision.APPROVE: "Пост одобрен. Уйдёт в группу ближайшей публикацией.",
    Decision.CANCEL: "Публикация отменена, пост снова ждёт решения.",
    Decision.IMAGES: "Картинки будут нарисованы заново, сцены те же.",
    Decision.SCENES: "Сцены придумаются заново, текст остаётся.",
    Decision.TEXT: "Текст будет написан заново, и картинки вместе с ним.",
    Decision.TRASH: "Пост выброшен, тема вернулась в конец очереди.",
    Decision.TRASH_TOPIC: "Пост и тема выброшены.",
    Decision.RETRY: "Пост возвращён в работу с чистым счётом попыток.",
}


@router.post("/api/posts/{post_id}/decision", response_model=Applied)
def decide(
    post_id: int,
    body: DecisionRequest,
    conn: sqlite3.Connection = Depends(deps.session),
) -> Applied:
    _post(conn, post_id)

    if not decisions.apply(conn, post_id, body.decision, version=body.version):
        _refuse(conn, post_id)

    row = _post(conn, post_id)
    return Applied(
        ok=True,
        state=row["state"],
        state_label=deps.label_of(row["state"]),
        what_next=WHAT_NEXT[body.decision] + deps.worker_note(conn),
    )


@router.post("/api/posts/{post_id}/text", response_model=Applied)
def edit_text(
    post_id: int,
    body: EditRequest,
    conn: sqlite3.Connection = Depends(deps.session),
) -> Applied:
    """Правка текста и заголовка.

    Заголовок печатается на обложке, поэтому его смена стоит пересборки и
    повторной отправки картинок. Смена только текста — ни того, ни другого, и
    сказать об этом надо до нажатия, а не после.
    """
    current = _post(conn, post_id)
    title = body.title if body.title is not None else current["title"]
    edit = edits.Edit(
        title=title,
        body=body.body,
        cover_changes=bool(title and title != current["title"]),
    )

    if not edits.apply(conn, post_id, edit):
        _refuse(conn, post_id)

    row = _post(conn, post_id)
    return Applied(
        ok=True,
        state=row["state"],
        state_label=deps.label_of(row["state"]),
        what_next=(
            "Заголовок изменён — обложка соберётся заново, картинки не меняются."
            if edit.cover_changes
            else "Текст сохранён. Картинки остаются прежними, денег это не стоит."
        ) + (deps.worker_note(conn) if edit.cover_changes else ""),
    )


@router.post("/api/posts/{post_id}/version/{number}", response_model=Applied)
def switch_version(
    post_id: int, number: int, conn: sqlite3.Connection = Depends(deps.session)
) -> Applied:
    """Показать другой вариант поста.

    Переключать можно только пост, который ждёт решения. Иначе получается дыра,
    ради закрытия которой варианты и заводились: пост уехал на переделку, а
    вариант в базе подменился — и картинки нового варианта легли поверх файлов
    старого. Проверка состояния и подмена обязаны быть в одной транзакции.
    """
    _post(conn, post_id)

    with db.write_transaction(conn):
        # Проверка состояния ровно одна и ровно здесь — внутри той же
        # транзакции, что и подмена. Вторая, снаружи, выглядела аккуратнее и
        # давала более раннюю ошибку, но делала обе непроверяемыми: каждая
        # прикрывала другую, и мутация любой из них проходила мимо тестов.
        guard = conn.execute(
            "SELECT id FROM posts WHERE id = ? AND state = ?", (post_id, State.IN_REVIEW)
        ).fetchone()
        restored = guard is not None and versions.restore_within(conn, post_id, number)

    if not restored:
        _refuse(conn, post_id)

    row = _post(conn, post_id)
    return Applied(
        ok=True,
        state=row["state"],
        state_label=deps.label_of(row["state"]),
        what_next=f"Показан вариант {number}. Опубликовать можно любой из сохранённых.",
    )
