"""Решение владельца: одобрение, откаты, отказ.

Главная ловушка этапа — откат, который поменял состояние, но не стёр данные.
Шаги пропускают работу при готовых данных, поэтому такой откат выглядит рабочим
и молча возвращает владельцу ровно тот же пост, который он забраковал. Отсюда
двойная проверка на каждый откат: что данные стёрты И что следующий шаг
действительно взялся за работу.
"""

import json

import pytest

from factory.core import db
from factory.core.decisions import Decision, apply, approvals_in_a_row
from factory.core.models import State, TopicStatus
from factory.core.steps import handler_for


def post_row(conn, post_id):
    return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


def assets(conn, post_id):
    return conn.execute(
        "SELECT * FROM assets WHERE post_id = ? ORDER BY position", (post_id,)
    ).fetchall()


def rejections(conn, post_id):
    return conn.execute(
        "SELECT * FROM rejections WHERE post_id = ? ORDER BY id", (post_id,)
    ).fetchall()


@pytest.fixture
def in_review(pipeline):
    """Пост, доведённый до ревью честным прогоном всех шагов."""
    pipeline["advance_through"](
        State.QUEUED,
        State.TEXT_READY,
        State.FACTCHECKED,
        State.PROMPTS_READY,
        State.IMAGES_READY,
        State.COMPOSED,
    )
    # advance_through гоняет обработчики; состояние в базу пишет машина, которой
    # тут нет. Ставим последнее вручную — дальше проверяется именно решение.
    pipeline["context"](State.IN_REVIEW)
    # У поста в работе тема занята. В боевом коде её занимает создание поста,
    # здесь пост вставлен напрямую — иначе проверка «откат текста не освобождает
    # тему» сравнивала бы free с free и не значила бы ничего.
    with db.write_transaction(pipeline["conn"]):
        pipeline["conn"].execute(
            "UPDATE topics SET status = ? WHERE id = ?",
            (TopicStatus.TAKEN, pipeline["topic_id"]),
        )
    return pipeline


class TestApprove:
    def test_approve_moves_the_post_on(self, in_review):
        assert apply(in_review["conn"], in_review["post_id"], Decision.APPROVE, by=123456789)

        row = post_row(in_review["conn"], in_review["post_id"])
        assert row["state"] == State.APPROVED
        assert row["decided_by"] == 123456789
        assert row["decided_at"] is not None

    def test_approve_is_not_a_rejection(self, in_review):
        apply(in_review["conn"], in_review["post_id"], Decision.APPROVE)

        assert rejections(in_review["conn"], in_review["post_id"]) == []


class TestTrash:
    def test_the_topic_comes_back_for_another_try(self, in_review):
        apply(in_review["conn"], in_review["post_id"], Decision.TRASH)

        status = in_review["conn"].execute(
            "SELECT status FROM topics WHERE id = ?", (in_review["topic_id"],)
        ).fetchone()["status"]
        assert status == TopicStatus.FREE

    def test_the_post_is_rejected_with_a_snapshot(self, in_review):
        apply(in_review["conn"], in_review["post_id"], Decision.TRASH)

        assert post_row(in_review["conn"], in_review["post_id"])["state"] == State.REJECTED
        (row,) = rejections(in_review["conn"], in_review["post_id"])
        assert row["reason"] == "trash"

        # Снимок — будущий обучающий набор. Пустой снимок бесполезен.
        snapshot = json.loads(row["snapshot"])
        assert snapshot["post"]["body"]
        assert snapshot["assets"], "в снимок не попали промпты сцен"


class TestRollbackText:
    def test_the_text_is_erased(self, in_review):
        apply(in_review["conn"], in_review["post_id"], Decision.TEXT)

        row = post_row(in_review["conn"], in_review["post_id"])
        assert row["state"] == State.QUEUED
        assert row["title"] is None
        assert row["body"] is None
        assert row["question"] is None

    def test_the_stale_factcheck_verdict_is_erased(self, in_review):
        """Иначе новый текст поедет с проверкой от старого.

        Шаг фактчека пропускает работу при непустом вердикте: непочищенный
        вердикт означает, что новый текст вообще не будет проверен, а владельцу
        покажут чужое «ok».
        """
        assert post_row(in_review["conn"], in_review["post_id"])["factcheck_verdict"]

        apply(in_review["conn"], in_review["post_id"], Decision.TEXT)

        row = post_row(in_review["conn"], in_review["post_id"])
        assert row["factcheck_verdict"] is None
        assert row["factcheck_notes"] is None

    def test_the_topic_stays_taken(self, in_review):
        """Тема та же — переписывается текст, а не тема."""
        apply(in_review["conn"], in_review["post_id"], Decision.TEXT)

        status = in_review["conn"].execute(
            "SELECT status FROM topics WHERE id = ?", (in_review["topic_id"],)
        ).fetchone()["status"]
        assert status == TopicStatus.TAKEN

    def test_the_step_really_writes_a_new_text(self, in_review):
        """Проверка сторожа: шаг обязан взяться за работу, а не пройти мимо."""
        conn, post_id = in_review["conn"], in_review["post_id"]
        apply(conn, post_id, Decision.TEXT)
        before = in_review["providers"].llm.calls

        result, _ = in_review["run"](State.QUEUED)

        assert result.advanced
        assert in_review["providers"].llm.calls > before, "шаг текста пропустил работу"
        assert post_row(conn, post_id)["body"], "текст не записан заново"

    def test_the_factcheck_runs_again(self, in_review):
        conn, post_id = in_review["conn"], in_review["post_id"]
        apply(conn, post_id, Decision.TEXT)
        in_review["run"](State.QUEUED)
        before = in_review["providers"].factcheck.calls

        in_review["run"](State.TEXT_READY)

        assert in_review["providers"].factcheck.calls > before, "фактчек пропустил новый текст"


class TestRollbackScenes:
    def test_the_prompts_are_deleted(self, in_review):
        apply(in_review["conn"], in_review["post_id"], Decision.SCENES)

        assert post_row(in_review["conn"], in_review["post_id"])["state"] == State.FACTCHECKED
        assert assets(in_review["conn"], in_review["post_id"]) == []

    def test_the_step_really_invents_new_scenes(self, in_review):
        conn, post_id = in_review["conn"], in_review["post_id"]
        old = [row["prompt"] for row in assets(conn, post_id)]
        apply(conn, post_id, Decision.SCENES)
        before = in_review["providers"].llm.calls

        result, _ = in_review["run"](State.FACTCHECKED)

        assert result.advanced
        assert in_review["providers"].llm.calls > before, "шаг промптов пропустил работу"
        assert len(assets(conn, post_id)) == len(old)

    def test_the_text_survives(self, in_review):
        """Претензия к сценам — текст переписывать не за что."""
        conn, post_id = in_review["conn"], in_review["post_id"]
        body = post_row(conn, post_id)["body"]

        apply(conn, post_id, Decision.SCENES)

        assert post_row(conn, post_id)["body"] == body


class TestRollbackImages:
    def test_the_prompts_survive_but_the_files_are_dropped(self, in_review):
        conn, post_id = in_review["conn"], in_review["post_id"]
        prompts = [row["prompt"] for row in assets(conn, post_id)]

        apply(conn, post_id, Decision.IMAGES)

        assert post_row(conn, post_id)["state"] == State.PROMPTS_READY
        rows = assets(conn, post_id)
        assert [row["prompt"] for row in rows] == prompts
        assert all(row["local_path"] is None for row in rows)

    def test_the_seed_changes(self, in_review):
        """При том же seed модель вернёт ровно ту же картинку."""
        conn, post_id = in_review["conn"], in_review["post_id"]
        before = [row["seed"] for row in assets(conn, post_id)]

        apply(conn, post_id, Decision.IMAGES)

        after = [row["seed"] for row in assets(conn, post_id)]
        assert all(new != old for new, old in zip(after, before, strict=True))

    def test_the_composed_cover_mark_is_dropped(self, in_review):
        """Сборка обложки смотрит на метку в external_ref, а не на файл.

        Оставить метку — значит перерисовать картинки и всё равно выдать
        владельцу старую обложку.
        """
        conn, post_id = in_review["conn"], in_review["post_id"]
        cover = [row for row in assets(conn, post_id) if row["kind"] == "cover"][0]
        assert cover["external_ref"], "обложка не помечена собранной — тест не о том"

        apply(conn, post_id, Decision.IMAGES)

        cover = [row for row in assets(conn, post_id) if row["kind"] == "cover"][0]
        assert cover["external_ref"] is None

    def test_the_steps_really_redo_the_work(self, in_review):
        conn, post_id = in_review["conn"], in_review["post_id"]
        apply(conn, post_id, Decision.IMAGES)
        before = in_review["providers"].images.calls

        in_review["run"](State.PROMPTS_READY)

        assert in_review["providers"].images.calls > before, "шаг картинок пропустил работу"
        assert all(row["local_path"] for row in assets(conn, post_id))

    def test_the_cover_is_composed_again(self, in_review):
        conn, post_id = in_review["conn"], in_review["post_id"]
        apply(conn, post_id, Decision.IMAGES)
        in_review["run"](State.PROMPTS_READY)

        result, _ = in_review["run"](State.IMAGES_READY)

        assert result.advanced
        cover = [row for row in assets(conn, post_id) if row["kind"] == "cover"][0]
        assert cover["external_ref"], "обложка не пересобрана"


class TestGuards:
    @pytest.mark.parametrize("decision", list(Decision))
    def test_a_second_press_changes_nothing(self, in_review, decision):
        """Двойное нажатие и нажатие на старое сообщение обязаны быть безвредны."""
        conn, post_id = in_review["conn"], in_review["post_id"]
        assert apply(conn, post_id, decision) is True
        after_first = dict(post_row(conn, post_id))

        assert apply(conn, post_id, decision) is False

        assert dict(post_row(conn, post_id)) == after_first
        assert len(rejections(conn, post_id)) <= 1

    def test_a_post_not_in_review_is_refused(self, pipeline):
        conn, post_id = pipeline["conn"], pipeline["post_id"]

        assert apply(conn, post_id, Decision.APPROVE) is False

        assert post_row(conn, post_id)["state"] == State.QUEUED

    def test_the_retry_budget_is_reset(self, in_review):
        """Пост возвращается в работу с чистым счётом, а не с наследством."""
        conn, post_id = in_review["conn"], in_review["post_id"]
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET retry_count = 4, last_error = 'старая ошибка' WHERE id = ?",
                (post_id,),
            )

        apply(conn, post_id, Decision.TEXT)

        row = post_row(conn, post_id)
        assert row["retry_count"] == 0
        assert row["last_error"] is None
        assert row["next_attempt_at"] is None

    def test_the_album_marker_is_dropped(self, in_review):
        """Иначе пост вернётся на повторный просмотр вообще без картинок.

        Отметка «альбом уже отправляли» защищает от дублей внутри одной
        отправки. После отката это другая отправка, и картинки нужны заново —
        особенно после «Картинки заново», где их и просили перерисовать.
        """
        conn, post_id = in_review["conn"], in_review["post_id"]
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET review_album_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00Z", post_id),
            )

        apply(conn, post_id, Decision.IMAGES)

        assert post_row(conn, post_id)["review_album_at"] is None

    def test_the_stale_keyboard_reference_is_dropped(self, in_review):
        conn, post_id = in_review["conn"], in_review["post_id"]
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET review_message_id = 777 WHERE id = ?", (post_id,)
            )

        apply(conn, post_id, Decision.APPROVE)

        assert post_row(conn, post_id)["review_message_id"] is None


class TestApprovalStreak:
    def test_no_decisions_means_no_streak(self, pipeline):
        assert approvals_in_a_row(pipeline["conn"], pipeline["project_id"]) == 0

    def test_approvals_accumulate(self, in_review):
        conn = in_review["conn"]
        apply(conn, in_review["post_id"], Decision.APPROVE)

        assert approvals_in_a_row(conn, in_review["project_id"]) == 1

    def test_a_fixed_post_never_joins_the_streak(self, in_review):
        """Пост, который откатывали и потом одобрили, — это пост с правкой.

        Сравнение по времени тут не работает: отказ и одобрение попадают в одну
        секунду, и «одобрено после последнего отказа» даёт неверный ответ.
        """
        conn, post_id = in_review["conn"], in_review["post_id"]
        apply(conn, post_id, Decision.TEXT)
        with db.write_transaction(conn):
            conn.execute("UPDATE posts SET state = ? WHERE id = ?", (State.IN_REVIEW, post_id))

        apply(conn, post_id, Decision.APPROVE)

        assert approvals_in_a_row(conn, in_review["project_id"]) == 0

    def test_a_single_edit_resets_the_count(self, in_review):
        """«Подряд без единой правки» — значит любая правка обнуляет счёт."""
        conn, post_id = in_review["conn"], in_review["post_id"]
        apply(conn, post_id, Decision.APPROVE)
        assert approvals_in_a_row(conn, in_review["project_id"]) == 1

        with db.write_transaction(conn):
            conn.execute("UPDATE posts SET state = ? WHERE id = ?", (State.IN_REVIEW, post_id))
        apply(conn, post_id, Decision.TEXT)

        assert approvals_in_a_row(conn, in_review["project_id"]) == 0

    def test_only_the_posts_after_the_last_fix_are_counted(self, in_review):
        """Считать надо от свежих к старым, а не наоборот.

        Один давний пост с правкой не должен обнулять всё, что одобрено после
        него — иначе автоодобрение не включится больше никогда.
        """
        from tests.conftest import insert_post, insert_topic

        conn = in_review["conn"]
        project_id = in_review["project_id"]

        # Давняя правка.
        apply(conn, in_review["post_id"], Decision.TEXT)
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET state = ?, decided_at = ? WHERE id = ?",
                (State.IN_REVIEW, "2020-01-01T00:00:00Z", in_review["post_id"]),
            )
        apply(conn, in_review["post_id"], Decision.APPROVE)
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET decided_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00Z", in_review["post_id"]),
            )

        # Два чистых одобрения после неё.
        for number, stamp in enumerate(("2020-06-01T00:00:00Z", "2020-07-01T00:00:00Z"), start=2):
            topic = insert_topic(conn, project_id, f"Тема {number}")
            post = insert_post(conn, project_id, topic, idem_key=f"demo:{topic}:0")
            with db.write_transaction(conn):
                conn.execute(
                    "UPDATE posts SET state = ?, decided_at = ? WHERE id = ?",
                    (State.APPROVED, stamp, post),
                )

        assert approvals_in_a_row(conn, project_id) == 2

    def test_another_project_does_not_count(self, in_review):
        """Иначе чужая ниша включила бы автопубликацию в этой."""
        from tests.conftest import insert_project

        conn = in_review["conn"]
        apply(conn, in_review["post_id"], Decision.APPROVE)
        other = insert_project(conn, "другой")

        assert approvals_in_a_row(conn, other) == 0

    def test_approvals_of_another_project_do_not_add_up(self, in_review):
        """Проверка идёт через ветку с фильтром: у этого проекта отказ уже был.

        Без явного отказа считается другая ветка запроса, и подмена фильтра по
        проекту осталась бы незамеченной.
        """
        from tests.conftest import insert_post, insert_project, insert_topic

        conn = in_review["conn"]
        other = insert_project(conn, "другой")
        topic = insert_topic(conn, other, "Чужая тема")
        alien = insert_post(conn, other, topic, idem_key="другой:1:0")
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE posts SET decided_at = ? WHERE id = ?", ("2999-01-01T00:00:00Z", alien)
            )
        apply(conn, in_review["post_id"], Decision.APPROVE)

        assert approvals_in_a_row(conn, in_review["project_id"]) == 1
        assert approvals_in_a_row(conn, other) == 1


class TestSendForReview:
    """Отправка на ревью: делает её воркер, а не бот.

    Демо-проект живёт в режиме auto, поэтому все проверки здесь идут на копии
    конфига с режимом telegram — иначе шаг уходит в ветку «ревью пропущено» и
    ни одна из них ничего не проверяет.
    """

    @pytest.fixture
    def asking(self, pipeline):
        """Проект, который действительно спрашивает владельца."""
        from factory.core.config import TelegramCfg

        project = pipeline["project"]
        pipeline["asking_project"] = project.model_copy(
            update={
                "review": project.review.model_copy(update={"mode": "telegram"}),
                "telegram": TelegramCfg(
                    provider="stub", chat_id=123456789, reviewers=[123456789]
                ),
            }
        )
        return pipeline

    def run_send(self, pipeline, state=State.COMPOSED):
        ctx = pipeline["context"](state)
        ctx.project = pipeline["asking_project"]
        return handler_for(state)(ctx), ctx

    def test_the_post_is_sent_with_its_images(self, asking):
        asking["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )

        result, ctx = self.run_send(asking)

        assert result.advanced
        (sent,) = ctx.providers.notifier.sent
        assert sent["chat_id"] == 123456789
        assert sent["post_id"] == asking["post_id"]
        assert sent["body"]
        assert len(sent["images"]) == 4, "ушли не все картинки"

    def test_the_cover_goes_first(self, asking):
        """Владелец должен увидеть главное, не листая альбом."""
        asking["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )
        cover = asking["conn"].execute(
            "SELECT local_path FROM assets WHERE post_id = ? AND kind = 'cover'",
            (asking["post_id"],),
        ).fetchone()["local_path"]

        _, ctx = self.run_send(asking)

        assert ctx.providers.notifier.sent[0]["images"][0] == cover

    def test_the_message_id_is_remembered(self, asking):
        """Без него после решения нельзя убрать кнопки."""
        asking["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )

        self.run_send(asking)

        row = post_row(asking["conn"], asking["post_id"])
        assert row["review_chat_id"] == 123456789
        assert row["review_message_id"] is not None

    def test_the_album_is_not_sent_twice_after_a_timeout(self, asking):
        """Таймаут не значит «не дошло».

        На живом прогоне владелец получил один и тот же альбом из четырёх
        картинок три раза: ответ Telegram не успевал прийти, а повтор слал всё
        заново. Отметка ставится до отправки — второй раз картинки не уходят.
        """
        import httpx

        asking["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )
        ctx = asking["context"](State.COMPOSED)
        ctx.project = asking["asking_project"]
        notifier = ctx.providers.notifier
        working = notifier.send_for_review
        attempts = []

        def timeout(**kwargs):
            # Именно таймаут, а не любая ошибка: механизм повторов считает
            # временными сетевые сбои. С неповторяемой ошибкой возврат ретраев
            # остался бы незаметен, и тест ничего бы не проверял.
            attempts.append(kwargs["images"])
            raise httpx.ReadTimeout("Telegram не ответил вовремя")

        notifier.send_for_review = timeout
        with pytest.raises(httpx.ReadTimeout):
            handler_for(State.COMPOSED)(ctx)
        notifier.send_for_review = working

        # Повторов внутри шага быть не должно вовсе: ctx.post — снимок, и на
        # второй попытке отметка в нём всё ещё пуста, так что картинки ушли бы
        # заново. Именно так владелец и получил три одинаковых альбома.
        assert len(attempts) == 1, f"отправка повторялась {len(attempts)} раза"
        assert attempts[0], "в первой отправке не было картинок — тест не о том"

        result, ctx = self.run_send(asking)

        assert result.advanced
        assert ctx.providers.notifier.sent[0]["images"] == [], "картинки ушли второй раз"

    def test_a_failed_send_still_delivers_the_buttons(self, asking):
        """Потерять кнопки хуже, чем потерять картинки: пост застрянет навсегда."""
        from factory.core.errors import ProviderError

        asking["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )
        ctx = asking["context"](State.COMPOSED)
        ctx.project = asking["asking_project"]
        notifier = ctx.providers.notifier
        working = notifier.send_for_review
        notifier.send_for_review = lambda **kw: (_ for _ in ()).throw(
            ProviderError("Telegram не ответил вовремя")
        )
        with pytest.raises(ProviderError):
            handler_for(State.COMPOSED)(ctx)
        notifier.send_for_review = working

        result, ctx = self.run_send(asking)

        assert result.advanced
        assert len(ctx.providers.notifier.sent) == 1
        assert ctx.providers.notifier.sent[0]["body"], "текст с кнопками не ушёл"

    def test_a_repeat_does_not_send_twice(self, asking):
        """Иначе владелец получит один пост дважды и не поймёт, какой настоящий."""
        asking["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )
        _, ctx = self.run_send(asking)
        assert len(ctx.providers.notifier.sent) == 1

        result, ctx = self.run_send(asking)

        assert result.advanced
        assert len(ctx.providers.notifier.sent) == 1, "пост отправлен второй раз"

    def test_an_uncertain_factcheck_is_shown(self, asking):
        asking["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )
        with db.write_transaction(asking["conn"]):
            asking["conn"].execute(
                "UPDATE posts SET factcheck_verdict = 'uncertain', factcheck_notes = 'дата под вопросом' "
                "WHERE id = ?",
                (asking["post_id"],),
            )

        _, ctx = self.run_send(asking)

        warning = ctx.providers.notifier.sent[0]["warning"]
        assert "не уверен" in warning
        assert "дата под вопросом" in warning

    def test_a_clean_factcheck_shows_no_warning(self, asking):
        asking["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )
        with db.write_transaction(asking["conn"]):
            asking["conn"].execute(
                "UPDATE posts SET factcheck_verdict = 'ok', factcheck_notes = NULL WHERE id = ?",
                (asking["post_id"],),
            )

        _, ctx = self.run_send(asking)

        assert ctx.providers.notifier.sent[0]["warning"] is None

    def test_waiting_does_not_spend_the_retry_budget(self, asking):
        """Пост может ждать выходные — умирать от старости он не должен."""
        asking["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )
        self.run_send(asking)

        result, _ = self.run_send(asking, State.IN_REVIEW)

        assert result.outcome.value == "waiting"

    def test_nothing_is_sent_in_auto_mode(self, pipeline):
        """Режим auto — владельца не спрашивают, значит и не пишут ему."""
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )

        result, ctx = pipeline["run"](State.COMPOSED)

        assert result.advanced
        assert ctx.providers.notifier.sent == []

    def test_a_long_clean_streak_skips_the_question(self, asking):
        """auto_after_n_approved: подряд одобрено N постов — больше не спрашиваем."""
        from tests.conftest import insert_post, insert_topic

        conn, project_id = asking["conn"], asking["project_id"]
        needed = asking["asking_project"].review.auto_after_n_approved
        for number in range(needed):
            topic = insert_topic(conn, project_id, f"Тема {number}")
            post = insert_post(conn, project_id, topic, idem_key=f"demo:{topic}:0")
            with db.write_transaction(conn):
                conn.execute(
                    "UPDATE posts SET state = ?, decided_at = ? WHERE id = ?",
                    (State.PUBLISHED, "2020-01-01T00:00:00Z", post),
                )
        asking["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )

        result, ctx = self.run_send(asking)

        assert result.advanced
        assert ctx.providers.notifier.sent == [], "спросили, хотя счёт одобрений набран"

    def test_one_short_of_the_streak_still_asks(self, asking):
        """Проверка границы: N-1 одобрений — вопрос ещё задаётся."""
        from tests.conftest import insert_post, insert_topic

        conn, project_id = asking["conn"], asking["project_id"]
        needed = asking["asking_project"].review.auto_after_n_approved
        for number in range(needed - 1):
            topic = insert_topic(conn, project_id, f"Тема {number}")
            post = insert_post(conn, project_id, topic, idem_key=f"demo:{topic}:0")
            with db.write_transaction(conn):
                conn.execute(
                    "UPDATE posts SET state = ?, decided_at = ? WHERE id = ?",
                    (State.PUBLISHED, "2020-01-01T00:00:00Z", post),
                )
        asking["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )

        _, ctx = self.run_send(asking)

        assert len(ctx.providers.notifier.sent) == 1
