"""То, чего в системе не было до панели.

Четыре действия, каждое — новая возможность, а не обёртка над готовым:
подмена картинки своей, перерисовка одной по правленому промпту, порядок тем
перетаскиванием и две проверки — доступа к группе и внешности персонажа.

Две последние ходят в сеть прямо из запроса, и это осознанное исключение из
правила «панель ничего не выполняет». Оба вызова — явное действие владельца, а
не работа конвейера: он нажал «проверить» и ждёт ответа именно сейчас. Проба
персонажа при этом стоит денег, и её цена написана на кнопке.
"""

from __future__ import annotations

import base64
import sqlite3

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from factory.core import assets as core_assets
from factory.core import db, http
from factory.core import topics as core_topics
from factory.core.clock import now_utc, to_iso
from factory.core.errors import FactoryError
from factory.core.steps.prompts import scene_prompt
from factory.panel import deps
from factory.providers.registry import build_providers

router = APIRouter()


class Ordered(BaseModel):
    #: Полный список тем очереди в нужном порядке. Целиком, а не «переставь
    #: третью на первое место»: список короткий, а частичная перестановка
    #: требует знать, что происходило между чтением и записью.
    ids: list[int] = Field(min_length=1)


class Redraw(BaseModel):
    prompt: str | None = None


class Done(BaseModel):
    ok: bool
    what_next: str


class Access(BaseModel):
    ok: bool
    checked_at: str
    detail: str


class Preview(BaseModel):
    prompt: str
    image: str
    cost: float | None


def _project(conn: sqlite3.Connection, slug: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, slug FROM projects WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Проект «{slug}» не подключён.")
    return row


def _config(slug: str):
    configs = deps.projects()
    if slug not in configs:
        raise HTTPException(
            status_code=409,
            detail=f"Конфиг проекта «{slug}» не читается — сначала надо починить его.",
        )
    return configs[slug]


@router.put("/api/topics/{slug}/order", response_model=Done)
def reorder(
    slug: str, body: Ordered, conn: sqlite3.Connection = Depends(deps.session)
) -> Done:
    """Задать порядок очереди перетаскиванием.

    Переставляются только свободные темы этого проекта: воркер мог забрать одну
    из них в работу, пока владелец тащил мышкой, и переставлять её уже поздно —
    пост по ней пишется. Ограничение в самом запросе, а не проверкой до него:
    между проверкой и записью прошёл бы ещё один тик.
    """
    project = _project(conn, slug)

    with db.write_transaction(conn):
        for order, topic_id in enumerate(body.ids, start=1):
            conn.execute(
                "UPDATE topics SET position = ? "
                "WHERE id = ? AND project_id = ? AND status = 'free'",
                (order, topic_id, project["id"]),
            )

    return Done(ok=True, what_next="Порядок сохранён. Следующая тема берётся сверху.")


class NewTopics(BaseModel):
    #: Текст как есть, по теме в строке. Разбор на стороне сервера, потому что
    #: тем же разбором пользуется бот: два места, режущие строки по-разному,
    #: однажды разойдутся на пустой строке или дубле.
    text: str = Field(min_length=1)


class Added(BaseModel):
    ok: bool
    added: int
    skipped: int
    what_next: str


@router.post("/api/topics/{slug}", response_model=Added)
def add_topics(
    slug: str, body: NewTopics, conn: sqlite3.Connection = Depends(deps.session)
) -> Added:
    """Добавить темы списком в конец очереди."""
    project = _project(conn, slug)
    lines = [line.strip() for line in body.text.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=422, detail="В присланном тексте нет ни одной темы.")

    result = core_topics.add(conn, project["id"], lines)
    from factory.core import alerts

    # Тревога «скоро публиковать нечего» снимается там, где видно, что причина
    # исчезла: темы появились — значит и повод молчать тоже.
    alerts.clear(conn, "no_topics", slug)

    return Added(
        ok=True,
        added=result.added,
        skipped=result.skipped,
        what_next=(
            f"Добавлено тем: {result.added}."
            + (f" Повторов пропущено: {result.skipped}." if result.skipped else "")
        ),
    )


@router.post("/api/posts/{post_id}/image/{position}", response_model=Done)
def upload_image(
    post_id: int,
    position: int,
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(deps.session),
) -> Done:
    """Поставить свою картинку вместо сгенерированной.

    Обработчик синхронный намеренно. В асинхронном соединение с базой создаётся
    в рабочем потоке, а тело выполняется в цикле событий — SQLite такое
    запрещает, и падало это только на загрузке файла, потому что остальные
    ручки синхронные.
    """
    data = file.file.read()
    try:
        replaced = core_assets.replace(conn, post_id, position, data)
    except FactoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not replaced:
        raise HTTPException(
            status_code=409,
            detail="Картинку можно заменить только у поста, который ждёт решения.",
        )

    return Done(
        ok=True,
        what_next=(
            "Картинка заменена. Пост придёт на просмотр заново с новыми "
            "картинками — это займёт около минуты и денег не стоит."
        ) + deps.worker_note(conn),
    )


@router.post("/api/posts/{post_id}/redraw/{position}", response_model=Done)
def redraw_image(
    post_id: int,
    position: int,
    body: Redraw,
    conn: sqlite3.Connection = Depends(deps.session),
) -> Done:
    """Перерисовать одну картинку, при желании по правленому промпту."""
    if not core_assets.redraw(conn, post_id, position, body.prompt):
        raise HTTPException(
            status_code=409,
            detail="Перерисовать можно только картинку поста, который ждёт решения.",
        )

    return Done(
        ok=True,
        what_next=(
            "Картинка будет нарисована заново. Остальные три не тронуты."
            + deps.worker_note(conn)
        ),
    )


@router.post("/api/groups/{slug}/check", response_model=Access)
def check_access(slug: str, conn: sqlite3.Connection = Depends(deps.session)) -> Access:
    """Проверить, что ключи работают и группа отвечает.

    Ответ честный: «проверено в 18:02, публикация разрешена» либо то, что
    ответил ВК, своими словами. Зелёная галочка без проверки хуже её отсутствия.
    """
    config = _config(slug)
    _project(conn, slug)
    from factory.core.config import resolve_secret

    stamp = to_iso(now_utc())
    try:
        token = resolve_secret(config.vk.token_env, context=f"публикации в группу {slug}")
        with http.client_for("vk", proxy_env=config.vk.proxy_env) as client:
            response = client.get(
                "https://api.vk.com/method/groups.getById",
                params={
                    "group_id": config.vk.group_id,
                    "access_token": token,
                    "v": config.vk.api_version,
                },
            )
        payload = response.json()
    except FactoryError as exc:
        return Access(ok=False, checked_at=stamp, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — проверка связи не должна ронять экран
        return Access(ok=False, checked_at=stamp, detail=f"Не удалось связаться с ВК: {exc}")

    if "error" in payload:
        return Access(
            ok=False,
            checked_at=stamp,
            detail=f"ВКонтакте отказал: {payload['error'].get('error_msg', 'без объяснения')}",
        )
    return Access(ok=True, checked_at=stamp, detail="Ключ принят, группа отвечает.")


class PreviewRequest(BaseModel):
    #: Приметы. Пусто — берутся из конфига проекта.
    character: str | None = None
    scene: str = Field(min_length=1)


@router.post("/api/groups/{slug}/preview", response_model=Preview)
def preview_character(
    slug: str, body: PreviewRequest, conn: sqlite3.Connection = Depends(deps.session)
) -> Preview:
    """Одна пробная картинка по приметам.

    Стоит денег, и это единственное место в панели, где деньги тратятся по
    нажатию. Зато подбирать внешность целым постом — это впятеро дороже и в
    тридцать раз дольше.
    """
    config = _config(slug)
    _project(conn, slug)

    prompt = scene_prompt(
        body.character if body.character is not None else config.image.character,
        body.scene,
        config.image.scene_style,
    )
    try:
        providers = build_providers(config)
        data = providers.images.generate(prompt)
    except FactoryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    from factory.core.retry import cost_of

    return Preview(
        prompt=prompt,
        # Картинка возвращается в самом ответе: класть пробу в хранилище постов
        # значило бы мусорить там файлами, которые никому не принадлежат.
        image="data:image/png;base64," + base64.b64encode(data).decode(),
        cost=cost_of(data),
    )
