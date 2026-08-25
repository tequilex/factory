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
    review_keyboard,
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

NOT_YOURS = "Эта кнопка не для вас."
ALREADY_DONE = "По этому посту решение уже принято."
ALREADY_OUT = "Пост уже вышел в группу — отменить нельзя. Удалить его можно только в самой группе."


def _refusal(conn: sqlite3.Connection, post_id: int, decision: Decision) -> str:
    """Почему нажатие не сработало. Общее «уже принято» тут вводит в заблуждение."""
    row = conn.execute("SELECT external_id FROM posts WHERE id = ?", (post_id,)).fetchone()
    if decision is Decision.CANCEL and row is not None and row["external_id"]:
        return ALREADY_OUT
    return ALREADY_DONE


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

    @dispatcher.message(Command("pause"))
    async def on_pause(message: Message) -> None:
        await message.answer(_switch(conn, current(), message.from_user.id, paused=True))

    @dispatcher.message(Command("resume"))
    async def on_resume(message: Message) -> None:
        await message.answer(_switch(conn, current(), message.from_user.id, paused=False))

    @dispatcher.callback_query(F.data.startswith("t:"))
    async def on_topics_answer(query: CallbackQuery) -> None:
        await _apply_pending_topics(conn, current(), query)

    @dispatcher.message(F.reply_to_message)
    async def on_edit(message: Message) -> None:
        """Ответ на сообщение поста — это исправленный текст."""
        await _accept_edit(conn, current(), message)

    @dispatcher.message(F.text & ~F.text.startswith("/") & ~F.text.contains("access_token="))
    async def on_topics_offer(message: Message) -> None:
        """Обычное сообщение — это, скорее всего, список тем.

        Переспрашиваем, а не добавляем сразу: случайно отправленное сообщение
        иначе молча попадёт в очередь и однажды выйдет постом.

        TODO: подтвердить у владельца. Если подтверждение окажется лишним
        шагом, убрать и добавлять сразу — откатить добавленное можно будет
        через /topics.
        """
        await _offer_topics(conn, current(), message)

    @dispatcher.message(F.text.contains("access_token="))
    async def on_vk_token(message: Message) -> None:
        """Владелец прислал адрес после входа в ВК — вынуть ключ и сохранить.

        Принимается и целый адрес из строки браузера, и один ключ: просить
        человека вырезать подстроку из длинного адреса на телефоне — верный
        способ получить ключ, обрезанный на символ.
        """
        await _accept_vk_token(conn, current(), message)

    @dispatcher.callback_query(F.data.startswith("r:"))
    async def on_decision(query: CallbackQuery) -> None:
        parsed = parse_callback(query.data or "")
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
        # Иначе выбор между вариантами не значил бы ничего: в группу всё равно
        # уходил бы самый свежий.
        if decision is Decision.APPROVE and version is not None:
            if not versions.restore(conn, post_id, version):
                await query.answer(
                    "Этот вариант больше недоступен.", show_alert=True
                )
                return

        if not apply(conn, post_id, decision, by=query.from_user.id):
            await query.answer(
                _refusal(conn, post_id, decision), show_alert=True
            )
            return

        await query.answer(f"{ICON[decision]} {LABEL[decision]}")
        await _replace_keyboard(query, decision, post_id, version or 1)
        await _confirm(query, decision, conn, current().get(slug or ""), slug)

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
        counts = topics.counts(conn, row["id"])
        lines = [
            f"[{slug}]",
            f"  свободных: {counts.free}",
            f"  в работе: {counts.taken}",
            f"  израсходовано: {counts.used}",
        ]
        upcoming = topics.upcoming(conn, row["id"])
        if upcoming:
            lines.append("\n  Ближайшие:")
            lines += [f"  • {title}" for title in upcoming]
            if counts.free > len(upcoming):
                lines.append(f"  …и ещё {counts.free - len(upcoming)}")
        else:
            lines.append("\n  Свободных тем нет. Пришлите новые списком, по теме в строке.")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


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


def _pending_key(user_id: int) -> str:
    return f"pending_topics:{user_id}"


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
    with db.write_transaction(conn):
        conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (_pending_key(message.from_user.id), json.dumps({"slug": slug, "lines": lines}), stamp),
        )

    preview = "\n".join(f"• {line}" for line in lines[:5])
    tail = f"\n…и ещё {len(lines) - 5}" if len(lines) > 5 else ""
    await message.answer(
        f"Добавить {_plural(len(lines))} в очередь [{slug}]?\n\n{preview}{tail}",
        reply_markup=_as_markup(
            {
                "inline_keyboard": [
                    [
                        {"text": "✅ Добавить", "callback_data": "t:add"},
                        {"text": "✖️ Не надо", "callback_data": "t:no"},
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

    key = _pending_key(query.from_user.id)
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    with db.write_transaction(conn):
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    await _drop_keyboard(query)

    if query.data == "t:no" or row is None:
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
        markup = _as_markup(cancel_keyboard(post_id))
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

    if paths.ignore_schedule():
        # Владелец включил режим «публиковать в любое время». Обещать слот
        # значит назвать час, до которого никто ждать не будет.
        return "✅ Уходит в группу ближайшим тиком: расписание отключено."

    when = next_slot_start(project, now_utc())
    if when is None:
        return "✅ Уходит в группу ближайшим тиком: расписание не задано."
    return f"✅ Уходит в группу {when:%d.%m в %H:%M} — это ближайший слот расписания."


async def _confirm(
    query: CallbackQuery,
    decision: Decision,
    conn: sqlite3.Connection,
    project: ProjectConfig | None = None,
    slug: str | None = None,
) -> None:
    text = {
        Decision.APPROVE: _approval_text(conn, project, slug),
        Decision.CANCEL: "↩️ Публикация отменена, пост снова ждёт вашего решения.",
        Decision.IMAGES: (
            "🔄 Картинки будут перерисованы, текст сохранён.\n\n"
            "Этот вариант никуда не делся: кнопка под ним осталась."
        ),
        Decision.SCENES: (
            "🎲 Сцены придумаются заново, текст сохранён.\n\n"
            "Этот вариант никуда не делся: кнопка под ним осталась."
        ),
        Decision.TEXT: (
            "✏️ Текст будет написан заново, тема остаётся.\n\n"
            "Этот вариант никуда не делся: кнопка под ним осталась."
        ),
        Decision.TRASH: "🗑 Пост выброшен, тема вернулась в очередь.",
        Decision.RETRY: (
            "🔧 Пост вернулся в работу с чистым счётом попыток.\n\n"
            "Если причина поломки не устранена, он сломается снова — тогда я "
            "напишу ещё раз."
        ),
    }[decision]
    try:
        await query.message.answer(text)
    except Exception as exc:  # noqa: BLE001 — то же самое: решение уже применено
        log.info("не удалось подтвердить решение", extra={"reason": str(exc)})


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
    asyncio.run(_poll(token, dispatcher))


async def _poll(token: str, dispatcher: Dispatcher) -> None:
    bot = Bot(token=token)
    try:
        # Накопившиеся за простой нажатия НЕ сбрасываем. Нажал владелец
        # «Опубликовать», пока бот лежал, — пост должен уйти, а не пропасть
        # молча. Устаревшие нажатия безвредны: apply() применяет решение только
        # к посту, который всё ещё в ревью, и отвечает, что решение уже принято.
        # delete_webhook нужен на случай, если когда-то был настроен вебхук:
        # с ним длинный опрос не работает.
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
