"""Applying the owner's decision to a post.

Separate from the bot on purpose: this is the part that must be right, and it is
testable without Telegram at all. The bot is a thin layer that turns a button
press into a call here.

Every rollback must **erase data, not just change state**. Steps skip their work
when the data already exists — that is what keeps a restart from paying twice for
the same text. The same guard makes a naive rollback a lie: the post returns to
``queued``, the text step sees a title and a body, skips, and the owner gets back
the exact post they rejected. So each rollback below clears precisely what the
step it returns to checks:

* ``text`` checks ``title and body`` — and ``factcheck`` checks the verdict, so a
  stale verdict would silently skip the check on a brand new text;
* ``prompts`` counts rows in ``assets``;
* ``images`` checks ``local_path``, and the same image comes back if ``seed``
  stays;
* ``compose`` checks a mark in ``external_ref`` — miss it and the old cover
  survives a full image regeneration;
* ``review`` checks ``review_album_at`` before sending pictures — miss it and the
  post comes back for a second look with no pictures at all, which is the one
  thing the owner needs in order to look.

Every decision is guarded by ``WHERE state = 'in_review'``. Pressing a button
twice, or pressing one on an old message, updates zero rows and changes nothing.
"""

from __future__ import annotations

import json
import random
import sqlite3
from enum import StrEnum

from factory.core import db
from factory.core.clock import now_utc, to_iso
from factory.core.logging import get_logger
from factory.core.models import Post, RejectionReason, State, TopicStatus

log = get_logger(__name__)

# Seeds are drawn from the same range the prompts step uses.
SEED_MAX = 2**31 - 1


class Decision(StrEnum):
    """Что владелец нажал. Значения уходят в callback_data — менять нельзя."""

    APPROVE = "ok"
    #: Передумал, пока пост ещё не вышел. Не отказ: править нечего, просто
    #: возврат к решению.
    CANCEL = "back"
    IMAGES = "img"
    SCENES = "scn"
    TEXT = "txt"
    TRASH = "del"
    #: Починить сломанный пост: вернуть в цепочку с чистым счётом попыток.
    #: Не решение о содержании — поэтому не считается ни одобрением, ни отказом.
    RETRY = "fix"


#: Куда откатывается пост и по какой причине это записывается в ``rejections``.
#: ``None`` в причине — решение не является отказом.
TARGET_STATE: dict[Decision, State] = {
    Decision.APPROVE: State.APPROVED,
    Decision.CANCEL: State.IN_REVIEW,
    Decision.IMAGES: State.PROMPTS_READY,
    Decision.SCENES: State.FACTCHECKED,
    Decision.TEXT: State.QUEUED,
    Decision.TRASH: State.REJECTED,
    Decision.RETRY: State.QUEUED,
}

REJECTION_REASON: dict[Decision, RejectionReason | None] = {
    Decision.APPROVE: None,
    Decision.CANCEL: None,
    Decision.IMAGES: RejectionReason.IMAGES,
    Decision.SCENES: RejectionReason.SCENES,
    Decision.TEXT: RejectionReason.TEXT,
    Decision.TRASH: RejectionReason.TRASH,
    Decision.RETRY: None,
}

LABEL: dict[Decision, str] = {
    Decision.APPROVE: "Опубликовать",
    Decision.CANCEL: "Отменить публикацию",
    Decision.IMAGES: "Картинки заново",
    Decision.SCENES: "Другие сцены",
    Decision.TEXT: "Текст заново",
    Decision.TRASH: "В мусор",
    Decision.RETRY: "Попробовать снова",
}


def _next_version(conn: sqlite3.Connection, post_id: int) -> int:
    """Номер следующего варианта. Считается по уже сохранённым, а не по счётчику."""
    from factory.core.versions import next_number

    return next_number(conn, post_id)


def _snapshot(conn: sqlite3.Connection, post_id: int) -> str:
    """Что было в посте на момент отказа. Будущий обучающий набор.

    Формат тот же, что у отказа из командной строки: две колонки ``snapshot`` в
    одной таблице с разной структурой означали бы два разборщика для того, ради
    чего таблица и заведена.
    """
    from factory.core.reject import snapshot_of

    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return snapshot_of(conn, Post.from_row(row))

def _clear_for(conn: sqlite3.Connection, decision: Decision, post_id: int) -> None:
    """Стереть ровно то, на что смотрит шаг, куда пост возвращается."""
    if decision is Decision.TEXT:
        conn.execute(
            "UPDATE posts SET title = NULL, body = NULL, question = NULL, "
            "factcheck_verdict = NULL, factcheck_notes = NULL WHERE id = ?",
            (post_id,),
        )
        # Промпты и картинки сочинялись по старому тексту и к новому не подходят.
        conn.execute("DELETE FROM assets WHERE post_id = ?", (post_id,))

    elif decision is Decision.SCENES:
        conn.execute("DELETE FROM assets WHERE post_id = ?", (post_id,))

    elif decision is Decision.IMAGES:
        # Промпты остаются: претензия к рисунку, а не к замыслу. Меняется seed —
        # при том же значении модель вернёт ровно ту же картинку.
        for asset in conn.execute(
            "SELECT id FROM assets WHERE post_id = ?", (post_id,)
        ).fetchall():
            conn.execute(
                "UPDATE assets SET local_path = NULL, external_ref = NULL, seed = ? WHERE id = ?",
                (random.randint(1, SEED_MAX), asset["id"]),
            )


#: Из какого состояния решение вообще применимо. Одобрение и откаты — только
#: пока пост у владельца; отмена — только пока он одобрен и ещё не вышел.
ALLOWED_FROM: dict[Decision, State] = {
    Decision.APPROVE: State.IN_REVIEW,
    Decision.IMAGES: State.IN_REVIEW,
    Decision.SCENES: State.IN_REVIEW,
    Decision.TEXT: State.IN_REVIEW,
    Decision.TRASH: State.IN_REVIEW,
    Decision.CANCEL: State.APPROVED,
    Decision.RETRY: State.FAILED,
}


def apply(
    conn: sqlite3.Connection,
    post_id: int,
    decision: Decision,
    *,
    by: int | None = None,
    version: int | None = None,
) -> bool:
    """Применить решение. ``False`` — решение неприменимо, ничего не изменено.

    Всё одной транзакцией: снимок, очистка, смена состояния, возврат темы.
    Половина применённого решения хуже неприменённого — пост с пустым текстом
    в состоянии ``in_review`` не двинется ни в одну сторону.

    ``version`` — номер варианта, под которым нажали. Он восстанавливается
    здесь же, под той же проверкой состояния: снаружи это давало дыру, при
    которой отклонённое решение всё равно подменяло содержимое поста, а вместе
    с ним и папку, куда лягут следующие картинки.
    """
    target = TARGET_STATE[decision]
    reason = REJECTION_REASON[decision]
    stamp = to_iso(now_utc())

    with db.write_transaction(conn):
        # Отмена дополнительно требует, чтобы пост ещё не вышел: успели
        # опубликовать — отменять уже нечего, надо удалять в самой группе.
        row = conn.execute(
            "SELECT id, topic_id FROM posts WHERE id = ? AND state = ? "
            "AND (? != ? OR external_id IS NULL)",
            (post_id, ALLOWED_FROM[decision], decision, Decision.CANCEL),
        ).fetchone()
        if row is None:
            return False

        if version is not None and decision is Decision.APPROVE:
            from factory.core.versions import restore_within

            if not restore_within(conn, post_id, version):
                return False

        if reason is not None:
            conn.execute(
                "INSERT INTO rejections (post_id, reason, snapshot, created_at) "
                "VALUES (?, ?, ?, ?)",
                (post_id, reason, _snapshot(conn, post_id), stamp),
            )

        _clear_for(conn, decision, post_id)

        if decision is Decision.TRASH:
            # Тема не потрачена: по ней будет новый пост с другим idem_key.
            conn.execute(
                "UPDATE topics SET status = ? WHERE id = ?",
                (TopicStatus.FREE, row["topic_id"]),
            )

        # Сбрасываются и счётчик попыток, и время следующей: пост возвращается
        # в работу немедленно и с чистым бюджетом, а не с наследством от того,
        # что владелец забраковал. Сообщение с кнопками больше не актуально.
        # decided_at у починки не ставится: это не решение о содержании, а
        # «попробуй ещё раз». Проставить его значило бы засчитать сломанный пост
        # в счёт одобрений подряд и приблизить автопубликацию без ревью.
        touches_decision = decision not in (Decision.RETRY, Decision.CANCEL)

        if decision in (Decision.APPROVE, Decision.CANCEL):
            # Сообщение остаётся тем же: с него владелец отменяет публикацию,
            # если передумал, и на нём же появится ссылка на вышедший пост.
            #
            # У отмены decided_at не ставится: решения по посту снова нет, а
            # непустая метка засчитала бы его в «одобрено подряд без правок» и
            # приблизила автопубликацию без ревью.
            conn.execute(
                "UPDATE posts SET state = ?, retry_count = 0, last_error = NULL, "
                "next_attempt_at = NULL, decided_at = ?, decided_by = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    target,
                    stamp if touches_decision else None,
                    by if touches_decision else None,
                    stamp, post_id,
                ),
            )
        else:
            # Откат — это заявка на новый вариант, а не правка нынешнего.
            # Номер растёт, и картинки лягут в свою папку, не затирая прежние.
            # Новый вариант заводят только откаты. Мусор переделывать нечего,
            # а починка возвращает в работу ровно то, что уже сделано, — иначе
            # владелец потерял бы вариант, который сломался на последнем шаге.
            #
            # TODO: подтвердить у владельца. Если «Попробовать снова» чаще будет
            # означать «сделай заново, этот не удался», то вариант надо
            # наращивать. Выбрано осторожное: не терять то, что уже готово.
            fresh = decision not in (Decision.TRASH, Decision.RETRY)
            version = _next_version(conn, post_id) if fresh else None
            conn.execute(
                "UPDATE posts SET state = ?, retry_count = 0, last_error = NULL, "
                "next_attempt_at = NULL, review_message_id = NULL, review_album_at = NULL, "
                "review_album_message_id = NULL, version = COALESCE(?, version), "
                "decided_at = COALESCE(?, decided_at), decided_by = COALESCE(?, decided_by), "
                "updated_at = ? WHERE id = ?",
                (
                    target, version,
                    stamp if touches_decision else None,
                    by if touches_decision else None,
                    stamp, post_id,
                ),
            )

    if decision is Decision.TRASH:
        # Выброшенный пост никогда не опубликуется, а чистка файлов висела
        # только на публикации. С вариантами это стало заметно: каждый откат
        # оставляет ещё одну папку с картинками.
        _forget_files(post_id)

    _forget_alerts(conn, post_id)

    log.info(
        "решение владельца применено",
        extra={"post_id": post_id, "decision": str(decision), "state": str(target), "by": by},
    )
    return True


def _forget_files(post_id: int) -> None:
    """Удалить картинки поста. Сбой здесь только логируется.

    Решение уже применено и записано; отказаться от него из-за неудалённого
    файла было бы куда хуже, чем оставить файл на диске.
    """
    import shutil

    from factory.core import paths

    try:
        shutil.rmtree(paths.post_tmp_dir(post_id))
    except FileNotFoundError:
        pass
    except OSError as exc:  # noqa: BLE001 — диск не повод отменять решение
        log.warning("не удалось убрать картинки", extra={"post_id": post_id, "reason": str(exc)})


def _forget_alerts(conn: sqlite3.Connection, post_id: int) -> None:
    """Снять тревоги по посту: он снова в работе, и всё начинается заново.

    Без этого «Попробовать снова» чинит пост один раз: отметка о поломке
    остаётся висеть, и вторая поломка того же поста проходит молча. Бот при
    этом прямо обещает написать ещё раз — то есть обещание не выполняется.
    То же с застрявшим постом, который откатили.
    """
    from factory.core import alerts

    row = conn.execute(
        "SELECT p.slug FROM projects p JOIN posts o ON o.project_id = p.id WHERE o.id = ?",
        (post_id,),
    ).fetchone()
    if row is None:
        return
    for name in ("failed", "stuck"):
        alerts.clear(conn, name, f"{row['slug']}:{post_id}")


def approvals_in_a_row(conn: sqlite3.Connection, project_id: int) -> int:
    """Сколько постов одобрено подряд без единой правки.

    Считается запросом, а не счётчиком в базе: счётчик пришлось бы наращивать и
    обнулять в трёх местах, и он разошёлся бы с реальностью при первом же
    откате. На данных ответ точен всегда.

    Сравнивать метки времени отказа и одобрения нельзя: они пишутся с точностью
    до секунды, и два решения, принятые подряд, оказываются одновременными.
    Поэтому идём по постам от последнего решённого и останавливаемся на первом,
    которому потребовалась правка. Пост, который откатывали и потом одобрили,
    правку потребовал — он обрывает счёт, а не продолжает его.
    """
    rows = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM rejections r WHERE r.post_id = p.id) AS was_fixed "
        "FROM posts p WHERE p.project_id = ? AND p.decided_at IS NOT NULL "
        "ORDER BY p.decided_at DESC, p.id DESC",
        (project_id,),
    ).fetchall()

    streak = 0
    for row in rows:
        if row["was_fixed"]:
            break
        streak += 1
    return streak
