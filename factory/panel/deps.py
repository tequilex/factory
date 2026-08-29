"""Общее для всех ручек панели: база, проекты, подписи состояний.

База открывается на запрос и закрывается после него. Для SQLite это дёшево, а
держать одно соединение на весь процесс — значит однажды получить длинную
транзакцию из панели, которая держит воркера. Правило по всему проекту одно:
писать короткими транзакциями, а панель тем более пишет редко.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from factory.core import db, lock
from factory.core.config import ProjectConfig, available_slugs, load_project
from factory.core.errors import FactoryError
from factory.core.logging import get_logger
from factory.core.models import State
from factory.core.topics import STATE_WORDS

log = get_logger(__name__)

#: Подписи состояний для человека. Берутся из ``core/topics.py``, а не пишутся
#: заново: бот и панель обязаны называть одно и то же одинаково, иначе владелец
#: увидит «ждёт решения» в боте и что-нибудь своё в панели.
#:
#: Здесь только то, чего в общем словаре нет: терминальные состояния боту не
#: нужны, а панель их показывает.
STATE_LABELS: dict[str, str] = {
    **STATE_WORDS,
    State.PUBLISHED: "опубликован",
    State.REJECTED: "выброшен",
}


def label_of(state: str) -> str:
    """Подпись состояния. Незнакомое отдаётся как есть, а не прячется.

    Пустая строка на новом состоянии выглядела бы как отсутствие данных, и
    искать причину пришлось бы в интерфейсе, а не в том месте, где состояние
    завели и забыли подписать.
    """
    return STATE_LABELS.get(state, state)


def session() -> Iterator[sqlite3.Connection]:
    """Соединение с базой на один запрос.

    ``cross_thread`` обязателен: FastAPI разрешает зависимость в одном потоке
    пула, а обработчик выполняет в другом, и SQLite это запрещает. Проявляется
    только при параллельных запросах — то есть на живом экране, а не в тестах.
    """
    conn = db.connect(cross_thread=True)
    try:
        yield conn
    finally:
        conn.close()


def worker_note(conn: sqlite3.Connection) -> str:
    """Приписка к ответу на действие, когда выполнять его некому.

    Панель ничего не выполняет: она ставит отметку, а работу делает воркер.
    Пока он молчит, любое «будет нарисовано» и «уйдёт в группу» — обещание,
    которое никто не выполнит.

    Поймано на живом экране: владелец отправил картинку на перерисовку и
    поставил свою, получил бодрые подтверждения и не дождался ничего. Обе
    команды при этом записались верно — работать было некому.
    """
    if not lock.heartbeat_is_stale(conn):
        return ""

    age = lock.heartbeat_age_sec(conn)
    сколько = "ни разу не отрабатывал" if age is None else f"молчит {int(age // 60)} мин"
    return (
        f" ⚠️ Но воркер {сколько} — выполнять задание сейчас некому. "
        "Оно не потеряно и выполнится, когда он вернётся."
    )


def projects() -> dict[str, ProjectConfig]:
    """Конфиги проектов, которые получилось загрузить.

    Битый конфиг одной ниши не должен закрывать панель целиком — ровно та же
    причина, что и у бота. Про непрочитанные проекты панель скажет отдельно,
    молчать о них нельзя: иначе группа просто пропадает с экрана.
    """
    loaded: dict[str, ProjectConfig] = {}
    for slug in available_slugs():
        try:
            loaded[slug] = load_project(slug)
        except FactoryError as exc:
            log.warning("проект пропущен", extra={"slug": slug, "reason": str(exc)})
    return loaded


def broken_projects() -> dict[str, str]:
    """Проекты, чей конфиг не читается, и почему."""
    broken: dict[str, str] = {}
    for slug in available_slugs():
        try:
            load_project(slug)
        except FactoryError as exc:
            broken[slug] = str(exc)
    return broken
