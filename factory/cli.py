"""Command line interface.

The owner does not read code, so this is one of the two places they actually
touch — the other is Telegram. Every command has a Russian help string, and every
error comes out as a :class:`FactoryError` with three parts rather than a
traceback.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import typer

from factory.core import config as config_module
from factory.core import db, lock, machine, paths
from factory.core.clock import now_utc, to_iso
from factory.core.errors import FactoryError
from factory.core.logging import setup_logging
from factory.core.models import Post, Project, State
from factory.core.reject import reject_post
from factory.workers import tick as tick_worker

app = typer.Typer(
    help="Контент-фабрика: автопостинг в сообщества ВКонтакте.",
    no_args_is_help=True,
    add_completion=False,
)

project_app = typer.Typer(help="Проекты (группы).", no_args_is_help=True)
topics_app = typer.Typer(help="Очередь тем.", no_args_is_help=True)
post_app = typer.Typer(help="Отдельные посты.", no_args_is_help=True)

app.add_typer(project_app, name="project")
app.add_typer(topics_app, name="topics")
app.add_typer(post_app, name="post")


def fail(error: FactoryError) -> None:
    """Print a human error and exit. Never a traceback."""
    typer.secho(str(error), fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def open_database() -> sqlite3.Connection:
    try:
        return db.open_db()
    except FactoryError as exc:
        fail(exc)


def load(slug: str):
    try:
        return config_module.load_project(slug)
    except FactoryError as exc:
        fail(exc)


def project_row(conn: sqlite3.Connection, slug: str) -> Project:
    row = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        known = [r["slug"] for r in conn.execute("SELECT slug FROM projects").fetchall()]
        fail(
            FactoryError(
                f"Проект '{slug}' не подключён к базе.",
                why=f"Подключённые проекты: {', '.join(known) or 'ни одного'}.",
                what_to_do=f"Подключи его: factory project add {slug}",
            )
        )
    return Project.from_row(row)


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Подробные логи.")) -> None:
    """Общие настройки для всех команд."""
    setup_logging("DEBUG" if verbose else "WARNING")
    config_module.load_env_file()


@app.command()
def init() -> None:
    """Создать базу данных и применить миграции. Выполняется один раз при установке."""
    conn = open_database()
    version = db.schema_version(conn)
    conn.close()
    typer.echo(f"База готова: {paths.db_path()} (версия схемы {version})")


@app.command()
def doctor() -> None:
    """Проверить, что всё на месте: каталоги, база, конфиги проектов."""
    problems: list[str] = []

    try:
        data_dir = paths.ensure_data_dir()
        typer.echo(f"✓ каталог данных: {data_dir}")
    except FactoryError as exc:
        fail(exc)

    try:
        conn = db.open_db()
    except FactoryError as exc:
        fail(exc)

    typer.echo(f"✓ база данных: {paths.db_path()} (схема {db.schema_version(conn)})")

    age = lock.heartbeat_age_sec(conn)
    if age is None:
        typer.echo("· тик ещё ни разу не отрабатывал")
    elif lock.heartbeat_is_stale(conn):
        problems.append(f"тик не отрабатывал {int(age // 60)} минут")
    else:
        typer.echo(f"✓ последний тик: {int(age // 60)} минут назад")

    held = conn.execute("SELECT value FROM meta WHERE key = 'tick_lock'").fetchone()
    if held:
        typer.echo("· сейчас идёт тик (или осталась блокировка после сбоя)")

    projects = machine.active_projects(conn)
    if not projects:
        problems.append("не подключено ни одного проекта")

    for project in projects:
        try:
            cfg = config_module.load_project(project.slug)
        except FactoryError as exc:
            problems.append(f"проект {project.slug}: {exc.what}")
            continue

        free = conn.execute(
            "SELECT COUNT(*) FROM topics WHERE project_id = ? AND status = 'free'",
            (project.id,),
        ).fetchone()[0]
        typer.echo(f"✓ проект {project.slug}: свободных тем {free}, буфер {cfg.limits.queue_buffer}")
        if free == 0:
            problems.append(f"у проекта {project.slug} закончились темы")

    if paths.ignore_schedule():
        problems.append(
            "включена FACTORY_IGNORE_SCHEDULE — посты публикуются в обход расписания. "
            "В боевом окружении её быть не должно"
        )

    conn.close()

    if problems:
        typer.secho("\nТребует внимания:", fg=typer.colors.YELLOW, err=True)
        for problem in problems:
            typer.secho(f"  · {problem}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)

    typer.secho("\nВсё в порядке.", fg=typer.colors.GREEN)


@app.command()
def unlock() -> None:
    """Снять зависшую блокировку тика. Нужно, если процесс убили и посты стоят."""
    conn = open_database()
    removed = lock.force_unlock(conn)
    conn.close()

    if removed:
        typer.echo("Блокировка снята. Следующий тик пойдёт как обычно.")
    else:
        typer.echo("Блокировки не было — снимать нечего.")


@app.command()
def run(
    once: bool = typer.Option(False, "--once", help="Один тик и выход. Для отладки."),
    loop: bool = typer.Option(False, "--loop", help="Бесконечный цикл. Режим контейнера."),
) -> None:
    """Запустить воркер: продвинуть посты по цепочке состояний."""
    if once == loop:
        fail(
            FactoryError(
                "Нужно выбрать ровно один режим запуска.",
                why="Указаны оба флага или ни одного.",
                what_to_do="factory run --once — один проход; factory run --loop — постоянно.",
            )
        )

    if loop:
        setup_logging("INFO")
        tick_worker.run_loop()
        return

    try:
        summary = tick_worker.run_once()
    except FactoryError as exc:
        fail(exc)

    if summary["skipped"]:
        typer.echo("Тик пропущен: уже работает другой процесс.")
        return
    typer.echo(
        f"Проектов: {summary['projects']}, создано постов: {summary['posts_created']}, "
        f"выполнено шагов: {summary['advanced']}"
    )


@project_app.command("add")
def project_add(slug: str = typer.Argument(..., help="Имя каталога в projects/.")) -> None:
    """Подключить проект к базе. Конфиг должен уже лежать в projects/<slug>/."""
    cfg = load(slug)
    conn = open_database()

    existing = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
    if existing:
        conn.close()
        typer.echo(f"Проект '{slug}' уже подключён — ничего делать не нужно.")
        return

    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO projects (slug, config_path, is_active, created_at) VALUES (?, ?, 1, ?)",
            (slug, str(config_module.project_dir(slug) / "config.yaml"), to_iso(now_utc())),
        )
    conn.close()
    typer.echo(
        f"Проект '{slug}' подключён. Публикаций в сутки: {cfg.limits.posts_per_day}, "
        f"постов в работе: {cfg.limits.queue_buffer}."
    )


@project_app.command("list")
def project_list() -> None:
    """Показать подключённые проекты."""
    conn = open_database()
    rows = conn.execute("SELECT * FROM projects ORDER BY id").fetchall()

    if not rows:
        typer.echo("Ни одного проекта не подключено. Подключить: factory project add demo")
    for row in rows:
        free = conn.execute(
            "SELECT COUNT(*) FROM topics WHERE project_id = ? AND status = 'free'", (row["id"],)
        ).fetchone()[0]
        mark = "включён" if row["is_active"] else "на паузе"
        typer.echo(f"{row['slug']:20} {mark:10} свободных тем: {free}")
    conn.close()


@project_app.command("pause")
def project_pause(slug: str) -> None:
    """Поставить проект на паузу: посты перестанут создаваться и двигаться."""
    conn = open_database()
    project_row(conn, slug)
    with db.write_transaction(conn):
        conn.execute("UPDATE projects SET is_active = 0 WHERE slug = ?", (slug,))
    conn.close()
    typer.echo(f"Проект '{slug}' на паузе. Ничего не удалено, снять: factory project resume {slug}")


@project_app.command("resume")
def project_resume(slug: str) -> None:
    """Снять проект с паузы."""
    conn = open_database()
    project_row(conn, slug)
    with db.write_transaction(conn):
        conn.execute("UPDATE projects SET is_active = 1 WHERE slug = ?", (slug,))
    conn.close()
    typer.echo(f"Проект '{slug}' снова в работе.")


@topics_app.command("import")
def topics_import(
    slug: str,
    path: Path = typer.Argument(..., help="Текстовый файл: одна тема на строку."),
) -> None:
    """Загрузить темы из файла. Пустые строки и повторы пропускаются."""
    if not path.is_file():
        fail(
            FactoryError(
                f"Файл с темами не найден: {path}",
                why="По этому пути ничего нет.",
                what_to_do="Проверь путь. Формат файла — одна тема на строку, кодировка UTF-8.",
            )
        )

    conn = open_database()
    project = project_row(conn, slug)

    existing = {
        row["title"]
        for row in conn.execute(
            "SELECT title FROM topics WHERE project_id = ?", (project.id,)
        ).fetchall()
    }

    added = skipped = 0
    with db.write_transaction(conn):
        for line in path.read_text(encoding="utf-8").splitlines():
            title = line.strip()
            if not title or title in existing:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO topics (project_id, title, status) VALUES (?, ?, 'free')",
                (project.id, title),
            )
            existing.add(title)
            added += 1
    conn.close()

    typer.echo(f"Загружено тем: {added}. Пропущено (пустые и повторы): {skipped}.")


@topics_app.command("list")
def topics_list(slug: str) -> None:
    """Показать, сколько тем осталось."""
    conn = open_database()
    project = project_row(conn, slug)
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM topics WHERE project_id = ? GROUP BY status",
        (project.id,),
    ).fetchall()
    conn.close()

    counts = {row["status"]: row["n"] for row in rows}
    typer.echo(
        f"свободных: {counts.get('free', 0)}, "
        f"в работе: {counts.get('taken', 0)}, "
        f"использованных: {counts.get('used', 0)}"
    )
    if not counts.get("free"):
        typer.secho(
            "Свободные темы закончились — новые посты создаваться не будут. "
            f"Добавить: factory topics import {slug} <файл>",
            fg=typer.colors.YELLOW,
        )


@post_app.command("create")
def post_create(slug: str) -> None:
    """Создать пост вручную, не дожидаясь пополнения очереди."""
    conn = open_database()
    project = project_row(conn, slug)
    post_id = machine.create_post_for_next_topic(conn, project)
    conn.close()

    if post_id is None:
        fail(
            FactoryError(
                f"У проекта '{slug}' нет свободных тем.",
                why="Все темы уже в работе или использованы.",
                what_to_do=f"Добавь темы: factory topics import {slug} <файл>",
            )
        )
    typer.echo(f"Создан пост {post_id}. Посмотреть: factory post show {post_id}")


@post_app.command("list")
def post_list(
    slug: str = typer.Argument(None, help="Имя проекта; без него — все проекты."),
    limit: int = typer.Option(20, help="Сколько последних постов показать."),
) -> None:
    """Показать последние посты и их состояния."""
    conn = open_database()
    if slug:
        project = project_row(conn, slug)
        rows = conn.execute(
            "SELECT * FROM posts WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project.id, limit),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()

    if not rows:
        typer.echo("Постов пока нет.")
        return

    for row in rows:
        post = Post.from_row(row)
        note = f"  ← {post.last_error.splitlines()[0]}" if post.last_error else ""
        typer.echo(f"{post.id:5}  {post.state:14}  {(post.title or '(без заголовка)')[:40]}{note}")


@post_app.command("show")
def post_show(post_id: int) -> None:
    """Показать всё, что известно о посте."""
    conn = open_database()
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        conn.close()
        fail(
            FactoryError(
                f"Пост {post_id} не найден.",
                why="Такого номера нет в базе.",
                what_to_do="Посмотри список: factory post list",
            )
        )

    post = Post.from_row(row)
    assets = conn.execute(
        "SELECT * FROM assets WHERE post_id = ? ORDER BY position", (post_id,)
    ).fetchall()
    cost = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE post_id = ?", (post_id,)
    ).fetchone()[0]
    slug = conn.execute(
        "SELECT slug FROM projects WHERE id = ?", (row["project_id"],)
    ).fetchone()["slug"]
    conn.close()

    tz = None
    try:
        tz = config_module.load_project(slug).vk.tz
    except FactoryError:
        # Показ поста не должен падать из-за битого конфига — время просто
        # останется в UTC.
        pass

    def when(moment) -> str:
        if moment is None:
            return "—"
        local = moment.astimezone(tz) if tz else moment
        return local.strftime("%d.%m.%Y %H:%M")

    typer.echo(f"Пост {post.id}   состояние: {post.state}")
    typer.echo(f"Заголовок: {post.title or '—'}")
    typer.echo(f"Вопрос:    {post.question or '—'}")
    if post.factcheck_verdict == "uncertain":
        typer.secho(f"⚠️  фактчек не уверен: {post.factcheck_notes}", fg=typer.colors.YELLOW)
    typer.echo(f"Картинок:  {len(assets)}")
    for asset in assets:
        path = asset["local_path"]
        if path and Path(path).is_file():
            mark = "на диске"
        elif post.state == State.PUBLISHED:
            # Ожидаемо: после публикации файлы удаляются, чтобы не копиться.
            mark = "удалена после публикации"
        else:
            mark = "ФАЙЛ ПРОПАЛ"
        typer.echo(f"  {asset['kind']:7} {asset['position']}  {mark:24} {path or ''}")
    typer.echo(f"Потрачено: ${cost:.4f}")
    if post.external_id:
        typer.echo(f"Опубликован: {when(post.published_at)}, номер {post.external_id}")
    elif post.next_attempt_at:
        typer.echo(f"Следующая попытка: {when(post.next_attempt_at)}")
    if post.last_error:
        typer.secho(f"Последняя ошибка (попытка {post.retry_count}):", fg=typer.colors.RED)
        typer.echo(post.last_error)
    if post.body:
        typer.echo("\n--- текст ---")
        typer.echo(post.body)


@post_app.command("retry")
def post_retry(post_id: int) -> None:
    """Сбросить счётчик ошибок и вернуть пост в работу прямо сейчас."""
    conn = open_database()
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        conn.close()
        fail(
            FactoryError(
                f"Пост {post_id} не найден.",
                why="Такого номера нет в базе.",
                what_to_do="Посмотри список: factory post list",
            )
        )

    post = Post.from_row(row)
    if post.state == State.PUBLISHED:
        conn.close()
        fail(
            FactoryError(
                f"Пост {post_id} уже опубликован, перезапускать нечего.",
                why=f"Он вышел {post.published_at} под номером {post.external_id}.",
                what_to_do="Если пост нужно убрать — удали его в самой группе ВКонтакте.",
            )
        )

    # failed — терминальное состояние; чтобы пост поехал дальше, его надо вернуть
    # в то состояние, на котором он сломался. Это последний успешный переход.
    target = post.state if post.state != State.FAILED else State.QUEUED
    with db.write_transaction(conn):
        conn.execute(
            "UPDATE posts SET state = ?, retry_count = 0, last_error = NULL, "
            "next_attempt_at = NULL, updated_at = ? WHERE id = ?",
            (target, to_iso(now_utc()), post_id),
        )
    conn.close()

    if post.state == State.FAILED:
        typer.echo(f"Пост {post_id} возвращён в начало цепочки и поедет со следующего тика.")
    else:
        typer.echo(f"Пост {post_id} снова в работе, состояние '{target}'.")


@post_app.command("reject")
def post_reject(
    post_id: int,
    reason: str = typer.Option("trash", help="Причина: text | images | trash."),
) -> None:
    """Выбросить пост. Тема вернётся в очередь и будет использована заново."""
    conn = open_database()
    try:
        reject_post(conn, post_id, reason=reason)
    except FactoryError as exc:
        conn.close()
        fail(exc)
    conn.close()
    typer.echo(f"Пост {post_id} выброшен, тема возвращена в очередь.")


@app.command()
def stats(
    slug: str = typer.Argument(None, help="Имя проекта; без него — все."),
    days: int = typer.Option(30, help="За сколько последних дней считать."),
) -> None:
    """Показать, что происходило: посты, ошибки, потраченные деньги."""
    conn = open_database()
    since = to_iso(now_utc().replace(microsecond=0)) if days <= 0 else None
    params: list = []
    where = ""
    if slug:
        project = project_row(conn, slug)
        where = " WHERE project_id = ?"
        params = [project.id]

    by_state = conn.execute(
        f"SELECT state, COUNT(*) AS n FROM posts{where} GROUP BY state ORDER BY n DESC", params
    ).fetchall()
    spent = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM runs").fetchone()[0]
    failures = conn.execute("SELECT COUNT(*) FROM runs WHERE ok = 0").fetchone()[0]
    rejections = conn.execute(
        "SELECT reason, COUNT(*) AS n FROM rejections GROUP BY reason"
    ).fetchall()
    conn.close()

    typer.echo("Посты по состояниям:")
    for row in by_state:
        typer.echo(f"  {row['state']:14} {row['n']}")
    typer.echo(f"\nНеудачных вызовов: {failures}")
    typer.echo(f"Потрачено всего:   ${spent:.4f}")
    if rejections:
        typer.echo("Отклонено:")
        for row in rejections:
            typer.echo(f"  {row['reason']:8} {row['n']}")


def cli() -> None:
    """Console entry point. Turns any stray FactoryError into a clean message."""
    try:
        app()
    except FactoryError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
