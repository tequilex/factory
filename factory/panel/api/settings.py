"""Настройки группы: расписание, лимиты, модели, персонаж, промпты, обложка.

Панель правит тот же ``config.yaml``, который читает воркер каждый проход.
Отдельного хранилища настроек нет намеренно: упадёт панель — система продолжит
работать, а файл останется читаемым и правимым руками.

Цена этого решения — ответственность за файл. Всё, что связано с проверкой,
атомарностью и сохранением комментариев, живёт в ``core/config_write.py``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from factory.core import config_write
from factory.core.errors import FactoryError
from factory.panel import deps

router = APIRouter()

#: Разделы, которые панель показывает и даёт править. Список закрыт: поле,
#: не попавшее сюда, панель не покажет и не запишет.
#:
#: Закрыт намеренно. Открытый означал бы, что любая новая настройка появляется
#: в интерфейсе сама, без единой подписи и объяснения, — а владелец не читает
#: код и понять её сможет только по названию.
EDITABLE = ("vk", "llm", "image", "content", "review", "telegram", "limits", "persona")


class Settings(BaseModel):
    slug: str
    #: Файл целиком, как он лежит на диске: панель показывает его в разделе
    #: «для тех, кто хочет посмотреть», а фронт правит поля по отдельности.
    raw: str
    values: dict[str, Any]


class Change(BaseModel):
    #: Только разделы из EDITABLE и только целиком: панель присылает раздел,
    #: слияние делает config_write.
    changes: dict[str, Any] = Field(min_length=1)


class TextFile(BaseModel):
    path: str = Field(min_length=1)
    text: str


class Saved(BaseModel):
    ok: bool
    what_next: str


def _known(slug: str, conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Проект «{slug}» не подключён.")


@router.get("/api/groups/{slug}/settings", response_model=Settings)
def read_settings(slug: str, conn: sqlite3.Connection = Depends(deps.session)) -> Settings:
    _known(slug, conn)
    from factory.core.config import project_dir

    path = project_dir(slug) / "config.yaml"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Файл настроек не найден: {path}")

    configs = deps.projects()
    config = configs.get(slug)
    values = (
        {name: getattr(config, name).model_dump(mode="json")
         for name in EDITABLE
         if getattr(config, name, None) is not None}
        if config is not None
        else {}
    )
    return Settings(slug=slug, raw=path.read_text(encoding="utf-8"), values=values)


@router.post("/api/groups/{slug}/settings/preview", response_model=Settings)
def preview_settings(
    slug: str, body: Change, conn: sqlite3.Connection = Depends(deps.session)
) -> Settings:
    """Как будет выглядеть файл после правки, с той же проверкой.

    Нужен, чтобы ошибка нашлась до нажатия «Сохранить». Владелец не читает код,
    и «поле llm.max_tokens должно быть числом» после того, как проект уже
    перестал загружаться, — это уже разбор аварии, а не подсказка.
    """
    _known(slug, conn)
    _guard_sections(body.changes)
    try:
        text = config_write.preview(slug, body.changes)
    except FactoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return Settings(slug=slug, raw=text, values=body.changes)


@router.post("/api/groups/{slug}/settings", response_model=Saved)
def save_settings(
    slug: str, body: Change, conn: sqlite3.Connection = Depends(deps.session)
) -> Saved:
    _known(slug, conn)
    _guard_sections(body.changes)
    try:
        config_write.update(slug, body.changes)
    except FactoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return Saved(
        ok=True,
        what_next=(
            "Настройки сохранены. Воркер перечитывает их каждый проход — "
            "перезапускать ничего не нужно."
        ),
    )


@router.post("/api/groups/{slug}/file", response_model=Saved)
def save_text_file(
    slug: str, body: TextFile, conn: sqlite3.Connection = Depends(deps.session)
) -> Saved:
    """Промпт голоса или пример стиля.

    Содержимое уходит в модель дословно — об этом фронт обязан предупредить
    рядом с полем, а не в подсказке где-то ниже.
    """
    _known(slug, conn)
    try:
        config_write.write_text_file(slug, body.path, body.text)
    except FactoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return Saved(ok=True, what_next="Сохранено. Подействует на следующий пост.")


@router.post("/api/groups/{slug}/reference", response_model=Saved)
def upload_reference(
    slug: str,
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(deps.session),
) -> Saved:
    """Новый эталонный портрет персонажа.

    Приводится к тому же размеру, что и картинки постов: образец другого
    формата модель принимает, но держит лицо заметно хуже.
    """
    _known(slug, conn)
    configs = deps.projects()
    config = configs.get(slug)
    if config is None or not config.image.reference:
        raise HTTPException(
            status_code=409,
            detail=(
                "У проекта не задан эталонный портрет. Сначала пропишите путь "
                "в image.reference — тогда его можно будет заменить."
            ),
        )

    from factory.core import assets as core_assets

    try:
        prepared = core_assets.fit(file.file.read())
        config_write.write_bytes_file(slug, config.image.reference, prepared)
    except FactoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return Saved(
        ok=True,
        what_next=(
            "Портрет заменён. Персонаж поменяется со следующего поста — "
            "уже готовые останутся прежними."
        ),
    )


def _guard_sections(changes: dict[str, Any]) -> None:
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Через панель нельзя менять: {', '.join(unknown)}. "
                f"Доступные разделы: {', '.join(EDITABLE)}."
            ),
        )
