"""Приём нажатий из Telegram.

Отдельный процесс (``factory bot``) и намеренно тонкий: вся логика решения живёт
в ``core/decisions.py`` и тестируется без Telegram вообще. Здесь только разбор
нажатия, проверка прав и ответ владельцу.

Разделение по процессам, а не по слоям, даёт два полезных свойства:

* упал бот — посты продолжают готовиться и приходить, кнопки просто не
  срабатывают, а Telegram придержит нажатия до суток и отдаст их при возврате;
* упал воркер — уже отправленные посты можно дожать кнопками.

База открывается синхронная, обработчики асинхронные. Это допустимо: запись
занимает миллисекунды, а очередь нажатий у одного человека не бывает длинной.
Заводить пул потоков ради одной кнопки в день значило бы усложнять то, что
работает.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from factory.core.clock import now_utc

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from factory.core import alerts, db, edits, paths, secrets, topics, versions
from factory.core.config import ProjectConfig, load_project, resolve_secret
from factory.core.decisions import LABEL, Decision, apply
from factory.core.errors import ConfigError, FactoryError
from factory.core.logging import get_logger
from factory.core.models import State
from factory.core.steps.publish import next_slot_start
from factory.providers.notifiers.telegram import (
    ICON,
    cancel_keyboard,
    extract_vk_token,
    parse_callback,
    retry_keyboard,
    ASK_TRASH,
    KEEP,
    review_keyboard,
    trash_keyboard,
    variant_keyboard,
)

log = get_logger(__name__)

START_TEXT = (
    "Это бот контент-фабрики.\n\n"
    "Сюда приходят готовые посты: сначала картинки, потом текст с кнопками. "
    "Нажатие сразу применяется — пост уходит в группу или возвращается на "
    "доработку.\n\n"
    "Команды:\n"
    "/status — что происходит прямо сейчас\n"
    "/topics — сколько тем осталось\n"
    "/pause — остановить выпуск, /resume — продолжить\n\n"
    "Чтобы добавить темы, просто пришлите их списком: по теме в строке.\n\n"
    "Текст можно поправить руками: ответьте на сообщение с постом своим "
    "вариантом, и он его заменит.\n\n"
    "Когда истечёт ключ загрузки картинок в ВК (это бывает раз в сутки), "
    "я пришлю ссылку — надо будет открыть её и переслать мне адрес из строки "
    "браузера."
)

#: Меню команд, которое Telegram показывает по нажатию «/». Без регистрации
#: выпадающего списка нет вовсе, и команды приходится помнить наизусть.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("status", "что происходит прямо сейчас"),
    ("topics", "сколько тем осталось"),
    ("schedule", "публиковать по расписанию или сразу"),
    ("schedule_on", "ждать слота расписания"),
    ("schedule_off", "публиковать сразу после одобрения"),
    ("pause", "остановить выпуск"),
    ("resume", "продолжить выпуск"),
    ("start", "что это за бот"),
)

NOT_YOURS = "Эта кнопка не для вас."
ALREADY_DONE = "По этому посту решение уже принято."
ALREADY_OUT = "Пост уже вышел в группу — отменить нельзя. Удалить его можно только в самой группе."


def _refusal(conn: sqlite3.Connection, post_id: int, decision: Decision) -> str:
    """Почему нажатие не сработало.

    Общее «уже принято» вводит в заблуждение: чаще всего решение принято не
    было, а пост просто уехал дальше или вернулся назад. Владелец должен
    понимать, что делать, а не гадать.
    """
    row = conn.execute(
        "SELECT state, external_id FROM posts WHERE id = ?", (post_id,)
    ).fetchone()
    if row is None:
        return ALREADY_DONE

    if decision is Decision.CANCEL and row["external_id"]:
        return ALREADY_OUT

    return {
        State.IN_REVIEW: "Пост снова ждёт решения — кнопки под ним обновил.",
        State.APPROVED: "Пост уже одобрен и ждёт публикации.",
        State.PUBLISHED: ALREADY_OUT,
        State.REJECTED: "Пост выброшен, тема вернулась в очередь.",
        State.FAILED: "Пост сломался. Кнопка починки — в сообщении о поломке.",
    }.get(row["state"], "Пост сейчас переделывается, решать пока нечего.")


async def _refresh_keyboard(
    conn: sqlite3.Connection, query: CallbackQuery, post_id: int, version: int
) -> None:
    """Показать под сообщением то, что с постом можно сделать сейчас."""
    row = conn.execute("SELECT state FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        return

    markup = None
    if row["state"] == State.IN_REVIEW:
        markup = _as_markup(review_keyboard(post_id, version))
    elif row["state"] == State.APPROVED:
        markup = _as_markup(cancel_keyboard(post_id, version))
    elif row["state"] == State.FAILED:
        markup = _as_markup(retry_keyboard(post_id))

    try:
        await query.message.edit_reply_markup(reply_markup=markup)
    except Exception as exc:  # noqa: BLE001 — косметика, решение уже отвергнуто
        log.info("не удалось обновить кнопки", extra={"reason": str(exc)})


def _looks_like_a_vk_key(message: Message) -> bool:
    """Есть ли в сообщении ключ ВК.

    Проверка тем же разборщиком, которым ключ потом достают, а не поиском
    подстроки «access_token=». Подстрока промахивалась на голом ключе — том
    самом, который разборщик принимает и о котором прямо сказано в подсказке
    владельцу.
    """
    return extract_vk_token(message.text or "") is not None


def _not_a_vk_key(message: Message) -> bool:
    """Обратное к :func:`_looks_like_a_vk_key`.

    Отдельной функцией, а не через `~`: обычную функцию aiogram принимает как
    фильтр, но отрицать её оператором нельзя — это работает только с его
    собственными объектами.
    """
    return not _looks_like_a_vk_key(message)


def _projects() -> dict[str, ProjectConfig]:
    """Конфиги всех проектов, у которых получилось загрузиться.

    Битый конфиг одной ниши не должен лишать владельца кнопок в остальных.
    """
    from factory.core.config import available_slugs

    loaded: dict[str, ProjectConfig] = {}
    for slug in available_slugs():
        try:
            loaded[slug] = load_project(slug)
        except FactoryError as exc:
            log.warning("проект пропущен", extra={"slug": slug, "reason": str(exc)})
    return loaded


def _project_of_post(conn: sqlite3.Connection, post_id: int) -> str | None:
    row = conn.execute(
        "SELECT p.slug FROM projects p JOIN posts o ON o.project_id = p.id WHERE o.id = ?",
        (post_id,),
    ).fetchone()
    return row["slug"] if row else None


def _may_press(projects: dict[str, ProjectConfig], slug: str | None, user_id: int) -> bool:
    """Право нажать даёт список проверяющих того проекта, чей это пост.

    Бот находится в Telegram обычным поиском. Без этой проверки кнопка
    «Опубликовать» в чужое сообщество доступна каждому, кто на бота наткнулся.
    """
    project = projects.get(slug or "")
    if project is None or project.telegram is None:
        return False
    return user_id in project.telegram.reviewers


def build_dispatcher(
    conn: sqlite3.Connection, projects: dict[str, ProjectConfig] | None = None
) -> Dispatcher:
    """Собрать обработчики. Отдельной функцией — чтобы тесты обошлись без сети.

    ``projects=None`` — читать конфиги заново на каждое нажатие. Так бот ведёт
    себя как воркер, который перечитывает их каждый тик: владелец правит список
    тех, кто одобряет посты, и это работает сразу. Иначе получалась ловушка —
    RUNBOOK советует добавить человека в список, человек жмёт кнопку и получает
    «эта кнопка не для вас», потому что бот помнит конфиг с момента запуска.

    Чтение — это разбор небольшого YAML, и происходит оно не чаще, чем человек
    нажимает кнопки.
    """
    dispatcher = Dispatcher()

    def current() -> dict[str, ProjectConfig]:
        return projects if projects is not None else _projects()

    @dispatcher.message(Command("start"))
    async def on_start(message: Message) -> None:
        await message.answer(START_TEXT)

    @dispatcher.message(Command("status"))
    async def on_status(message: Message) -> None:
        await message.answer(_status_text(conn, current(), message.from_user.id))

    @dispatcher.message(Command("topics"))
    async def on_topics(message: Message) -> None:
        await message.answer(_topics_text(conn, current(), message.from_user.id))

    @dispatcher.message(Command("schedule"))
    async def on_schedule(message: Message) -> None:
        await message.answer(_schedule_text(conn, current(), message.from_user.id))

    @dispatcher.callback_query(F.data.startswith("s:"))
    async def on_schedule_switch(query: CallbackQuery) -> None:
        await _switch_schedule(conn, current(), query)

    @dispatcher.message(Command("pause"))
    async def on_pause(message: Message) -> None:
        await message.answer(_switch(conn, current(), message.from_user.id, paused=True))

    @dispatcher.message(Command("schedule_on"))
    async def on_schedule_on(message: Message) -> None:
        await message.answer(_set_schedule(conn, current(), message.from_user.id, off=False))

    @dispatcher.message(Command("schedule_off"))
    async def on_schedule_off(message: Message) -> None:
        await message.answer(_set_schedule(conn, current(), message.from_user.id, off=True))

    @dispatcher.message(Command("resume"))
    async def on_resume(message: Message) -> None:
        await message.answer(_switch(conn, current(), message.from_user.id, paused=False))

    @dispatcher.callback_query(F.data.startswith("t:"))
    async def on_topics_answer(query: CallbackQuery) -> None:
        await _apply_pending_topics(conn, current(), query)

    @dispatcher.message(_looks_like_a_vk_key)
    async def on_vk_token(message: Message) -> None:
        """Ключ ВК ловится раньше всех остальных обработчиков.

        Иначе он уходит не туда, и оба промаха неприятны: голый ключ без
        адреса выглядел как список тем и становился темой поста, а ключ,
        присланный ответом на сообщение с тревогой (самое естественное
        действие в телефоне — ответить на то сообщение, где ссылка), уходил
        в правку текста. В обоих случаях ключ не сохранялся и оставался
        висеть в переписке.
        """
        await _accept_vk_token(conn, current(), message)

    @dispatcher.message(F.reply_to_message)
    async def on_edit(message: Message) -> None:
        """Ответ на сообщение поста — это исправленный текст."""
        await _accept_edit(conn, current(), message)

    @dispatcher.message(F.text & ~F.text.startswith("/"), _not_a_vk_key)
    async def on_topics_offer(message: Message) -> None:
        """Обычное сообщение — это, скорее всего, список тем.

        Переспрашиваем, а не добавляем сразу: случайно отправленное сообщение
        иначе молча попадёт в очередь и однажды выйдет постом.

        TODO: подтвердить у владельца. Если подтверждение окажется лишним
        шагом, убрать и добавлять сразу — откатить добавленное можно будет
        через /topics.
        """
        await _offer_topics(conn, current(), message)

    @dispatcher.callback_query(F.data.startswith("r:"))
    async def on_decision(query: CallbackQuery) -> None:
        raw = query.data or ""
        if await _handled_as_pseudo(conn, current(), query, raw):
            return

        parsed = parse_callback(raw)
        if parsed is None:
            await query.answer("Кнопка испорчена.", show_alert=True)
            return

        post_id, decision, version = parsed
        slug = _project_of_post(conn, post_id)

        if not _may_press(current(), slug, query.from_user.id):
            # Молчать нельзя: со стороны это неотличимо от поломки.
            log.warning(
                "нажатие от постороннего",
                extra={"post_id": post_id, "user_id": query.from_user.id},
            )
            await query.answer(NOT_YOURS, show_alert=True)
            return

        # Одобряют тот вариант, под которым нажали, а не последний сделанный.
        # Восстановление делает apply() под своей проверкой состояния: снаружи
        # это давало дыру, при которой отклонённое решение всё равно подменяло
        # содержимое поста.
        if not apply(conn, post_id, decision, by=query.from_user.id, version=version):
            await query.answer(_refusal(conn, post_id, decision), show_alert=True)
            # Кнопка устарела: состояние поста поменялось не через это
            # сообщение — из командной строки, другим сообщением или самим
            # воркером. Приводим клавиатуру к тому, что с постом можно делать
            # сейчас, иначе сообщение остаётся тупиком навсегда.
            await _refresh_keyboard(conn, query, post_id, version or 1)
            return

        await query.answer(f"{ICON[decision]} {LABEL[decision]}")
        await _replace_keyboard(query, decision, post_id, version or 1)
        await _confirm(query, decision, conn, current().get(slug or ""), slug, post_id)

    @dispatcher.errors()
    async def on_error(event, exception: Exception) -> bool:
        """Ошибка в одном нажатии не должна ронять бот целиком."""
        log.error("сбой в обработчике бота", extra={"reason": str(exception)})
        return True

    return dispatcher


async def _accept_vk_token(
    conn: sqlite3.Connection, projects: dict[str, ProjectConfig], message: Message
) -> None:
    mine = [
        (slug, project)
        for slug, project in projects.items()
        if project.telegram and message.from_user.id in project.telegram.reviewers
    ]
    if not mine:
        await message.answer(NOT_YOURS)
        return

    token = extract_vk_token(message.text or "")
    if token is None:
        await message.answer(
            "В сообщении нет ключа. Нужен весь адрес из строки браузера — "
            "тот, что начинается на https://oauth.vk.ru/blank.html#access_token=..."
        )
        return

    # Сообщение с ключом убирается из переписки сразу. Ключ даёт доступ к
    # сообществу; висеть в истории он не должен.
    await _forget(message)

    # Проектов может быть несколько, и переменная с ключом у каждого своя.
    # Записать в первую попавшуюся и снять тревогу у всех значило бы оставить
    # остальные со старым ключом и без предупреждения.
    updated: list[str] = []
    for slug, project in mine:
        name = project.vk.upload_token_env
        if not name:
            await message.answer(f"У проекта [{slug}] не задано поле vk.upload_token_env.")
            continue
        try:
            secrets.update_secret(name, token)
        except FactoryError as exc:
            await message.answer(str(exc))
            return
        alerts.clear(conn, "vk_token", slug)
        updated.append(name)
        log.info("ключ ВК обновлён владельцем", extra={"slug": slug, "name": name})

    if not updated:
        return
    name = ", ".join(sorted(set(updated)))

    await message.answer(
        f"Ключ принят и сохранён ({name}). Ваше сообщение я удалил.\n\n"
        "Публикация продолжится сама в ближайшую минуту — перезапускать ничего "
        "не нужно."
    )


def _mine(projects: dict[str, ProjectConfig], user_id: int) -> list[tuple[str, ProjectConfig]]:
    """Проекты, которые этот человек вправе смотреть и трогать."""
    return [
        (slug, project)
        for slug, project in projects.items()
        if project.telegram and user_id in project.telegram.reviewers
    ]


def _topics_text(
    conn: sqlite3.Connection, projects: dict[str, ProjectConfig], user_id: int
) -> str:
    mine = _mine(projects, user_id)
    if not mine:
        return NOT_YOURS

    blocks = []
    for slug, _ in mine:
        row = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            continue
        blocks.append(_one_project_topics(conn, slug, row["id"]))
    return "\n\n".join(blocks)


def _one_project_topics(conn: sqlite3.Connection, slug: str, project_id: int) -> str:
    """Три списка: что в запасе, что делается, что уже отработано.

    Числа без списков отвечают только на «сколько», а спрашивают обычно «а что
    именно» — чтобы понять, чем кормить систему дальше и не повторить тему.
    """
    counts = topics.counts(conn, project_id)
    lines = [f"[{slug}] тем всего: {counts.total}"]

    upcoming = topics.upcoming(conn, project_id)
    if upcoming:
        lines.append(f"\n📋 В запасе ({counts.free}), по очереди:")
        lines += [f"  {number}. {title}" for number, title in enumerate(upcoming, start=1)]
        lines += _tail(counts.free, len(upcoming))
    else:
        lines.append("\n📋 В запасе пусто. Пришлите новые темы списком, по теме в строке.")

    working = topics.in_progress(conn, project_id)
    if working:
        lines.append(f"\n⚙️ В работе ({counts.taken}):")
        lines += [f"  • {item.title} — {item.note}" for item in working]
        lines += _tail(counts.taken, len(working))

    finished = topics.done(conn, project_id)
    if finished:
        lines.append(f"\n✅ Отработано ({counts.used}), свежие сверху:")
        for item in finished:
            suffix = f" — {item.url}" if item.url else f" — {item.note}"
            lines.append(f"  • {item.title}{suffix}")
        lines += _tail(counts.used, len(finished))

    return "\n".join(lines)


def _tail(total: int, shown: int) -> list[str]:
    """Сколько ещё не поместилось. Молчание тут читается как «это всё»."""
    return [f"  …и ещё {total - shown}"] if total > shown else []


def _switch(
    conn: sqlite3.Connection, projects: dict[str, ProjectConfig], user_id: int, *, paused: bool
) -> str:
    mine = _mine(projects, user_id)
    if not mine:
        return NOT_YOURS

    for slug, _ in mine:
        topics.set_paused(conn, slug, paused)

    names = ", ".join(slug for slug, _ in mine)
    if paused:
        return (
            f"⏸ Остановлено: {names}.\n\n"
            "Новые посты не готовятся, публикаций не будет. Уже одобренные тоже "
            "подождут. Продолжить — /resume."
        )
    return f"▶️ Продолжаем: {names}."


def _set_schedule(
    conn: sqlite3.Connection, projects: dict[str, ProjectConfig], user_id: int, *, off: bool
) -> str:
    mine = _mine(projects, user_id)
    if not mine:
        return NOT_YOURS

    for slug, _ in mine:
        topics.set_schedule_off(conn, slug, off)

    if off:
        return (
            "⚡ Посты будут выходить сразу после одобрения, не дожидаясь слота.\n\n"
            "Дневной лимит при этом действует. Вернуть расписание — /schedule_on"
        )
    return "🕒 Посты будут ждать своего слота. Публиковать сразу — /schedule_off"


def _schedule_text(
    conn: sqlite3.Connection, projects: dict[str, ProjectConfig], user_id: int
) -> str:
    mine = _mine(projects, user_id)
    if not mine:
        return NOT_YOURS

    slug, project = mine[0]
    off = topics.schedule_is_off(conn, slug)
    slots = ", ".join(project.vk.schedule) or "не заданы"

    if off:
        return (
            f"[{slug}] Сейчас посты выходят **сразу** после одобрения.\n\n"
            f"Слоты расписания: {slots}.\n\n"
            "Включить расписание — /schedule_on"
        )
    return (
        f"[{slug}] Сейчас посты ждут слота расписания: {slots}.\n\n"
        "Публиковать сразу после одобрения — /schedule_off"
    )


async def _switch_schedule(
    conn: sqlite3.Connection, projects: dict[str, ProjectConfig], query: CallbackQuery
) -> None:
    if not _mine(projects, query.from_user.id):
        await query.answer(NOT_YOURS, show_alert=True)
        return

    off = (query.data or "").endswith(":off")
    for slug, _ in _mine(projects, query.from_user.id):
        topics.set_schedule_off(conn, slug, off)

    await _drop_keyboard(query)
    await query.answer("Готово")
    await query.message.answer(
        "⚡ Посты выходят сразу после одобрения."
        if off
        else "🕒 Посты ждут своего слота расписания."
    )


def _pending_key(marker: int | str) -> str:
    """Ключ по сообщению, а не по человеку.

    Два списка подряд иначе накладываются: второй затирает первый, кнопка под
    первым сообщением добавляет темы из второго, а кнопка под вторым отвечает
    «не добавляю».
    """
    return f"pending_topics:{marker}"


async def _offer_topics(
    conn: sqlite3.Connection, projects: dict[str, ProjectConfig], message: Message
) -> None:
    from factory.core.clock import now_utc as _now
    from factory.core.clock import to_iso as _iso

    mine = _mine(projects, message.from_user.id)
    if not mine:
        await message.answer(NOT_YOURS)
        return

    lines = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
    if not lines:
        return

    slug = mine[0][0]
    stamp = _iso(_now())
    # Метка едет в самой кнопке: иначе два списка подряд накладываются, и
    # кнопка под первым сообщением добавляет темы из второго.
    marker = message.message_id
    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (
                _pending_key(marker),
                json.dumps({"slug": slug, "lines": lines}),
                stamp,
            ),
        )

    preview = "\n".join(f"• {line}" for line in lines[:5])
    tail = f"\n…и ещё {len(lines) - 5}" if len(lines) > 5 else ""
    await message.answer(
        f"Добавить {_plural(len(lines))} в очередь [{slug}]?\n\n{preview}{tail}",
        reply_markup=_as_markup(
            {
                "inline_keyboard": [
                    [
                        {"text": "✅ Добавить", "callback_data": f"t:add:{marker}"},
                        {"text": "✖️ Не надо", "callback_data": f"t:no:{marker}"},
                    ]
                ]
            }
        ),
    )


def _plural(count: int) -> str:
    tail = "тем" if count % 10 == 0 or count % 10 >= 5 or 11 <= count % 100 <= 14 else (
        "тему" if count % 10 == 1 else "темы"
    )
    return f"{count} {tail}"


async def _apply_pending_topics(
    conn: sqlite3.Connection, projects: dict[str, ProjectConfig], query: CallbackQuery
) -> None:
    if not _mine(projects, query.from_user.id):
        await query.answer(NOT_YOURS, show_alert=True)
        return

    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("Кнопка испорчена.", show_alert=True)
        return

    key = _pending_key(parts[2])
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    with db.write_transaction(conn):
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    await _drop_keyboard(query)

    if parts[1] == "no" or row is None:
        await query.answer("Не добавляю.")
        return

    payload = json.loads(row["value"])
    project = conn.execute(
        "SELECT id FROM projects WHERE slug = ?", (payload["slug"],)
    ).fetchone()
    if project is None:
        await query.answer("Проект не подключён.", show_alert=True)
        return

    result = topics.add(conn, project["id"], payload["lines"])
    alerts.clear(conn, "no_topics", payload["slug"])
    await query.answer(f"Добавлено: {result.added}")
    await query.message.answer(
        f"Добавлено тем: {result.added}. Пропущено (повторы и пустые): {result.skipped}."
    )


async def _accept_edit(
    conn: sqlite3.Connection, projects: dict[str, ProjectConfig], message: Message
) -> None:
    post_id = edits.find_post_under(conn, message.reply_to_message.message_id)
    if post_id is None:
        await message.answer(
            "Не понял, к какому посту это относится. Ответьте на сообщение с "
            "постом — тем, под которым кнопки."
        )
        return

    slug = _project_of_post(conn, post_id)
    if not _may_press(projects, slug, message.from_user.id):
        await message.answer(NOT_YOURS)
        return

    edit = edits.parse(message.text or "")
    if edit is None:
        await message.answer("Пустое сообщение — править нечего.")
        return

    if not edits.apply(conn, post_id, edit):
        await message.answer(ALREADY_DONE)
        return

    if edit.cover_changes:
        await message.answer(
            f"Принял. Заголовок: «{edit.title}».\n\n"
            "Он печатается на обложке, поэтому обложку соберу заново — пост "
            "вернётся с новыми картинками через минуту."
        )
    else:
        await message.answer(
            f"Принял, {len(edit.body)} символов. Заголовок оставил прежним.\n\n"
            "Пост вернётся с кнопками через минуту. Чтобы поменять и заголовок, "
            "пришлите его первой строкой, потом пустую строку, потом текст."
        )


async def _forget(message: Message) -> None:
    """Удалить сообщение владельца с ключом. В личке боту это разрешено."""
    try:
        await message.delete()
    except Exception as exc:  # noqa: BLE001 — ключ уже сохранён, это гигиена
        log.info("не удалось удалить сообщение с ключом", extra={"reason": str(exc)})


async def _handled_as_pseudo(
    conn: sqlite3.Connection,
    projects: dict[str, ProjectConfig],
    query: CallbackQuery,
    raw: str,
) -> bool:
    """Нажатия, которые не решения: переспрос о мусоре и отказ от него.

    Разведены с решениями намеренно. Кнопка переспроса и кнопка подтверждения
    не могут слать одно и то же — иначе подтверждение снова откроет переспрос,
    и выбраться из него будет нельзя.
    """
    parts = raw.split(":")
    if len(parts) < 3 or parts[2] not in (ASK_TRASH, KEEP):
        return False

    post_id = int(parts[1]) if parts[1].isdigit() else None
    version = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    if post_id is None:
        await query.answer("Кнопка испорчена.", show_alert=True)
        return True

    slug = _project_of_post(conn, post_id)
    if not _may_press(projects, slug, query.from_user.id):
        await query.answer(NOT_YOURS, show_alert=True)
        return True

    if parts[2] == ASK_TRASH:
        await query.answer()
        await _replace_markup(query, trash_keyboard(post_id, version))
    else:
        await query.answer("Оставляю.")
        await _replace_markup(query, review_keyboard(post_id, version))
    return True


async def _replace_markup(query: CallbackQuery, keyboard: dict) -> None:
    """Поменять клавиатуру, не трогая сам текст сообщения."""
    try:
        await query.message.edit_reply_markup(reply_markup=_as_markup(keyboard))
    except Exception as exc:  # noqa: BLE001 — косметика
        log.info("не удалось поменять кнопки", extra={"reason": str(exc)})


async def _replace_keyboard(
    query: CallbackQuery, decision: Decision, post_id: int, version: int = 1
) -> None:
    """Поменять клавиатуру под тем, что теперь можно сделать.

    Одобрили — остаётся одна кнопка «Отменить публикацию»: пост ещё не вышел, и
    до ближайшего слота владелец вправе передумать. Отменили — возвращаются все
    решения.

    Откат — остаётся «Опубликовать этот вариант». В этом весь смысл вариантов:
    новый приходит отдельным сообщением, а прежний остаётся здесь и его можно
    выбрать обратно. Убрать кнопки значило бы вернуться к тому, от чего уходили,
    — посмотреть другой вариант ценой потери этого.

    В мусор — кнопок нет: пост выброшен, публиковать нечего.
    """
    markup = None
    if decision is Decision.APPROVE:
        markup = _as_markup(cancel_keyboard(post_id, version))
    elif decision is Decision.CANCEL:
        markup = _as_markup(review_keyboard(post_id, version))
    elif decision in (Decision.TEXT, Decision.SCENES, Decision.IMAGES):
        markup = _as_markup(variant_keyboard(post_id, version))

    try:
        await query.message.edit_reply_markup(reply_markup=markup)
    except Exception as exc:  # noqa: BLE001 — решение уже применено, это косметика
        log.info("не удалось поменять кнопки", extra={"reason": str(exc)})


def _as_markup(keyboard: dict):
    """Словарь в формате Bot API — в объект aiogram."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(**button) for button in row]
            for row in keyboard["inline_keyboard"]
        ]
    )


async def _drop_keyboard(query: CallbackQuery) -> None:
    """Снять кнопки совсем: решение уже принято, нажимать больше нечего."""
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:  # noqa: BLE001 — решение уже применено, это косметика
        log.info("не удалось убрать кнопки", extra={"reason": str(exc)})


def _todays_count(
    conn: sqlite3.Connection, project: ProjectConfig, slug: str
) -> tuple[int, int]:
    """Сколько постов вышло сегодня и сколько разрешено."""
    from factory.core.steps.publish import published_today

    row = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        return 0, project.limits.posts_per_day
    return published_today(conn, project, row["id"]), project.limits.posts_per_day


def _approval_text(conn: sqlite3.Connection, project: ProjectConfig | None, slug: str | None) -> str:
    """Что ответить на «Опубликовать».

    Обещать публикацию, когда ключ ВК истёк, нельзя: владелец нажимает, видит
    бодрое «уходит» и тишину, а тревога о ключе уже висит и второй раз не
    придёт. Про время тоже честно: «в ближайший слот» без часа читается как
    «сейчас», и пауза до вечера выглядит поломкой.
    """
    if project is None or slug is None:
        return "✅ Пост одобрен."

    if alerts.is_raised(conn, "vk_token", slug):
        link = alerts.vk_token_url(project.vk.app_id)
        tail = f"\n\n{link}" if link else "\n\nКак получить ключ — RUNBOOK.md."
        return (
            "✅ Пост одобрен, но выйти сейчас не может: ключ загрузки картинок "
            "в ВК истёк.\n\nПришлите мне новый — пост уедет сам, повторно "
            "нажимать не нужно." + tail
        )

    if topics.is_paused(conn, slug):
        # Проект на паузе: тик его не видит, и обещать час публикации значит
        # опровергать собственное сообщение о паузе.
        return (
            "✅ Пост одобрен, но выпуск на паузе — он подождёт.\n\n"
            "Продолжить: /resume"
        )

    published, allowed = _todays_count(conn, project, slug)
    if published >= allowed:
        # Лимит про количество, а не про время: при отключённом расписании он
        # тоже действует. Обещать «уходит ближайшим тиком» значит соврать на
        # сутки — ровно так владелец и решил, что система сломалась.
        return (
            f"✅ Пост одобрен, но сегодня уже вышло {published} из {allowed} — "
            "он уедет завтра.\n\n"
            "Поменять: limits.posts_per_day в конфиге проекта."
        )

    if topics.schedule_is_off(conn, slug):
        # Спрашиваем ровно то же, что спрашивает шаг публикации. Смотреть только
        # на переменную окружения было мало: владелец выключал расписание
        # командой в боте, а подтверждение всё равно обещало слот — то есть
        # опровергало сообщение, которое бот прислал минуту назад.
        return "✅ Уходит в группу ближайшим тиком: расписание отключено."

    when = next_slot_start(project, now_utc())
    if when is None:
        return "✅ Уходит в группу ближайшим тиком: расписание не задано."
    return f"✅ Уходит в группу {when:%d.%m в %H:%M} — это ближайший слот расписания."


#: Решения, после которых пост уезжает переделываться. Только у них есть
#: ожидание: одобрение и мусор ничего не готовят.
REMAKES = (Decision.TEXT, Decision.SCENES, Decision.IMAGES)


def _remember_waiting(conn: sqlite3.Connection, post_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    with db.write_transaction(conn):
        conn.execute(
            "UPDATE posts SET waiting_message_id = ? WHERE id = ?", (message_id, post_id)
        )


async def _confirm(
    query: CallbackQuery,
    decision: Decision,
    conn: sqlite3.Connection,
    project: ProjectConfig | None = None,
    slug: str | None = None,
    post_id: int | None = None,
) -> None:
    text = {
        Decision.APPROVE: _approval_text(conn, project, slug),
        Decision.CANCEL: "↩️ Публикация отменена, пост снова ждёт вашего решения.",
        Decision.IMAGES: (
            "🔄 Перерисовываю картинки, текст сохраняю.\n"
            "Вернусь через пару минут с новым вариантом.\n\n"
            "Этот вариант никуда не делся: кнопка под ним осталась."
        ),
        Decision.SCENES: (
            "🎲 Придумываю сцены заново, текст сохраняю.\n"
            "Вернусь через пару минут с новым вариантом.\n\n"
            "Этот вариант никуда не делся: кнопка под ним осталась."
        ),
        Decision.TEXT: (
            "✏️ Пишу текст заново, тема остаётся.\n"
            "Вернусь через пару минут с новым вариантом.\n\n"
            "Этот вариант никуда не делся: кнопка под ним осталась."
        ),
        Decision.TRASH: (
            "🗑 Пост выброшен. Тема вернулась в очередь, но в конец — "
            "новый пост по ней появится не сразу."
        ),
        Decision.TRASH_TOPIC: "🚫 Пост выброшен, тема закрыта. Больше по ней не пишем.",
        Decision.RETRY: (
            "🔧 Пост вернулся в работу с чистым счётом попыток.\n\n"
            "Если причина поломки не устранена, он сломается снова — тогда я "
            "напишу ещё раз."
        ),
    }[decision]
    try:
        sent = await query.message.answer(text)
    except Exception as exc:  # noqa: BLE001 — то же самое: решение уже применено
        log.info("не удалось подтвердить решение", extra={"reason": str(exc)})
        return

    if decision in REMAKES:
        # Это же сообщение и есть «делаю»: оно висит, пока готовится новый
        # вариант, и убирается, когда тот приходит. Номер — в базу, потому что
        # между откатом и результатом проходит пара минут, а бота могут
        # перезапустить: иначе сообщение останется висеть навсегда, и владелец
        # будет думать, что работа идёт до сих пор.
        _remember_waiting(conn, post_id, getattr(sent, "message_id", None))


def _status_text(
    conn: sqlite3.Connection, projects: dict[str, ProjectConfig], user_id: int
) -> str:
    """Короткая сводка по тем проектам, которые этот человек ревьюит."""
    mine = [
        slug
        for slug, project in projects.items()
        if project.telegram and user_id in project.telegram.reviewers
    ]
    if not mine:
        return NOT_YOURS

    lines: list[str] = []
    for slug in mine:
        row = conn.execute(
            "SELECT "
            "SUM(state = ?) AS waiting, "
            "SUM(state = ?) AS approved, "
            "SUM(state = ?) AS failed, "
            "SUM(state NOT IN (?, ?, ?, ?, ?)) AS working "
            "FROM posts o JOIN projects p ON p.id = o.project_id WHERE p.slug = ?",
            (
                State.IN_REVIEW, State.APPROVED, State.FAILED,
                # approved и in_review уже посчитаны отдельными строками выше:
                # без них в этом списке один пост попадал бы в две строки сразу.
                State.PUBLISHED, State.REJECTED, State.FAILED, State.IN_REVIEW,
                State.APPROVED,
                slug,
            ),
        ).fetchone()
        free = conn.execute(
            "SELECT COUNT(*) FROM topics t JOIN projects p ON p.id = t.project_id "
            "WHERE p.slug = ? AND t.status = 'free'",
            (slug,),
        ).fetchone()[0]
        lines.append(
            f"[{slug}]\n"
            f"  ждут вашего решения: {row['waiting'] or 0}\n"
            f"  одобрены, ждут слота: {row['approved'] or 0}\n"
            f"  готовятся: {row['working'] or 0}\n"
            f"  сломались: {row['failed'] or 0}\n"
            f"  свободных тем: {free}"
        )
    return "\n\n".join(lines)


#: Как часто бот проверяет, жив ли воркер. Чаще незачем: порог измеряется
#: минутами, а лишние проверки — лишние записи в лог.
WATCH_EVERY_SEC = 60


def check_worker(conn: sqlite3.Connection, projects: dict[str, ProjectConfig]) -> None:
    """Сказать владельцу, если воркер перестал отвечать.

    Следит именно бот: воркер, который встал, не может пожаловаться на себя
    сам. Это самая обидная поломка из всех — кнопки работают, подтверждения
    приходят, а выполнять решения некому, и снаружи одно от другого неотличимо.
    """
    from factory.core import lock
    from factory.providers.registry import build_providers

    silent = lock.heartbeat_is_stale(conn)
    minutes = int((lock.heartbeat_age_sec(conn) or 0) // 60)

    for slug, project in projects.items():
        if project.telegram is None:
            continue
        try:
            notifier = build_providers(project).notifier
        except FactoryError as exc:
            log.warning("нет чем уведомить", extra={"slug": slug, "reason": str(exc)})
            continue

        if silent:
            alerts.raise_once(
                conn, notifier, chat_id=project.telegram.chat_id,
                name="worker_silent", scope=slug,
                text=alerts.worker_silent_text(minutes),
            )
        elif alerts.is_raised(conn, "worker_silent", slug):
            # Тревога снимается там, где видно, что причина исчезла, и владелец
            # узнаёт об этом: молча погашенная тревога оставляет его в
            # уверенности, что всё ещё сломано.
            alerts.clear(conn, "worker_silent", slug)
            try:
                notifier.alert(
                    chat_id=project.telegram.chat_id, text=alerts.worker_back_text()
                )
            except Exception as exc:  # noqa: BLE001 — уведомление не работа
                log.info("не удалось сообщить о возврате", extra={"reason": str(exc)})


async def _watch_worker(conn: sqlite3.Connection) -> None:
    """Фоновая проверка. Живёт столько же, сколько бот."""
    import asyncio as _asyncio

    while True:
        await _asyncio.sleep(WATCH_EVERY_SEC)
        try:
            check_worker(conn, _projects())
        except Exception:  # noqa: BLE001 — наблюдатель не имеет права ронять бота
            log.exception("проверка воркера сорвалась")


def run() -> None:
    """Запустить бота длинным опросом. Блокирует процесс."""
    projects = _projects()
    if not projects:
        raise ConfigError(
            "Не загрузился ни один проект.",
            why="В каталоге проектов нет ни одного читаемого config.yaml.",
            what_to_do="Проверь: factory doctor",
        )

    tokens = {
        project.telegram.token_env
        for project in projects.values()
        if project.telegram is not None
    }
    if not tokens:
        raise ConfigError(
            "Ни один проект не настроен на ревью через Telegram.",
            why="Ни в одном config.yaml нет секции telegram.",
            what_to_do=(
                "Добавь её и поставь review.mode: telegram, либо не запускай бота — "
                "в режиме auto он не нужен."
            ),
        )
    if len(tokens) > 1:
        raise ConfigError(
            "Проекты просят разные токены бота.",
            why=f"Найдены переменные: {', '.join(sorted(tokens))}.",
            what_to_do=(
                "Бот один на все проекты. Оставь одно значение telegram.token_env "
                "во всех конфигах."
            ),
        )

    token = resolve_secret(next(iter(tokens)), context="бота в Telegram")
    conn = db.connect()
    db.migrate(conn)
    # Без словаря: конфиги перечитываются на каждое нажатие.
    dispatcher = build_dispatcher(conn)

    log.info("бот запущен", extra={"projects": sorted(projects)})
    asyncio.run(_poll(token, dispatcher, conn))


async def _publish_menu(bot: Bot) -> None:
    """Показать список команд в выпадашке по «/».

    Регистрируется при каждом запуске: список меняется вместе с ботом, а
    Telegram помнит прошлый до перезаписи. Сбой здесь не повод не работать —
    команды продолжат приниматься набором вручную.
    """
    from aiogram.types import BotCommand

    try:
        await bot.set_my_commands(
            [BotCommand(command=name, description=text) for name, text in COMMANDS]
        )
    except Exception as exc:  # noqa: BLE001 — меню это удобство, не работа
        log.warning("не удалось обновить меню команд", extra={"reason": str(exc)})


async def _poll(token: str, dispatcher: Dispatcher, conn: sqlite3.Connection) -> None:
    bot = Bot(token=token)
    watcher = asyncio.create_task(_watch_worker(conn))
    try:
        # Накопившиеся за простой нажатия НЕ сбрасываем. Нажал владелец
        # «Опубликовать», пока бот лежал, — пост должен уйти, а не пропасть
        # молча. Устаревшие нажатия безвредны: apply() применяет решение только
        # к посту, который всё ещё в ревью, и отвечает, что решение уже принято.
        # delete_webhook нужен на случай, если когда-то был настроен вебхук:
        # с ним длинный опрос не работает.
        await bot.delete_webhook(drop_pending_updates=False)
        await _publish_menu(bot)
        await dispatcher.start_polling(bot)
    finally:
        watcher.cancel()
        await bot.session.close()
