"""Варианты поста: сделать ещё один, не потеряв предыдущий.

Смысл возможности — выбор. Поэтому здесь проверяется не «вариант записался», а
то, ради чего всё делалось: что предыдущий вариант можно достать целиком и
опубликовать именно его, включая картинки.
"""

import json
from pathlib import Path

import pytest

from factory.core import db, paths, versions
from factory.core.config import TelegramCfg
from factory.core.decisions import Decision, apply
from factory.core.models import Post, State
from factory.core.steps import handler_for


def post_row(conn, post_id):
    return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


def assets_of(conn, post_id):
    return conn.execute(
        "SELECT * FROM assets WHERE post_id = ? ORDER BY position", (post_id,)
    ).fetchall()


@pytest.fixture
def asking(pipeline):
    """Проект, который спрашивает владельца, и пост, доведённый до ревью."""
    project = pipeline["project"]
    pipeline["asking_project"] = project.model_copy(
        update={
            "review": project.review.model_copy(update={"mode": "telegram"}),
            "telegram": TelegramCfg(provider="stub", chat_id=123456789, reviewers=[123456789]),
        }
    )

    def to_review():
        pipeline["advance_through"](
            State.QUEUED, State.TEXT_READY, State.FACTCHECKED,
            State.PROMPTS_READY, State.IMAGES_READY,
        )
        ctx = pipeline["context"](State.COMPOSED)
        ctx.project = pipeline["asking_project"]
        result = handler_for(State.COMPOSED)(ctx)
        assert result.advanced
        pipeline["context"](State.IN_REVIEW)
        return ctx

    pipeline["to_review"] = to_review
    return pipeline


class TestRecording:
    def test_the_first_pass_makes_variant_one(self, asking):
        asking["to_review"]()

        assert versions.count(asking["conn"], asking["post_id"]) == 1

    def test_a_rollback_makes_a_second_variant(self, asking):
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()

        apply(conn, post_id, Decision.TEXT)
        asking["to_review"]()

        assert versions.count(conn, post_id) == 2
        assert post_row(conn, post_id)["version"] == 2

    def test_repeating_the_same_pass_does_not_duplicate(self, asking):
        """Отправка могла сорваться и повториться — вариант от этого не удвоится."""
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()

        versions.record(conn, Post.from_row(post_row(conn, post_id)))

        assert versions.count(conn, post_id) == 1

    def test_a_repeat_refreshes_what_was_stored(self, asking):
        """Текст могли поправить руками — вариант обязан совпасть с показанным.

        Правка не заводит новый вариант: менялся тот же самый. Значит запись под
        тем же номером должна обновить содержимое, а не сохранить устаревшее.
        """
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        with db.write_transaction(conn):
            conn.execute("UPDATE posts SET body = ? WHERE id = ?", ("поправлено рукой", post_id))

        versions.record(conn, Post.from_row(post_row(conn, post_id)))

        stored = conn.execute(
            "SELECT body FROM post_versions WHERE post_id = ? AND number = 1", (post_id,)
        ).fetchone()["body"]
        assert stored == "поправлено рукой"

    def test_variants_are_counted_per_post(self, asking):
        """Иначе чужие варианты пронумеровали бы этот пост «третьим из пяти»."""
        from tests.conftest import insert_post, insert_topic

        conn = asking["conn"]
        asking["to_review"]()
        other_topic = insert_topic(conn, asking["project_id"], "Другая тема")
        other = insert_post(conn, asking["project_id"], other_topic, idem_key="demo:9:0")
        versions.record(conn, Post.from_row(post_row(conn, other)))

        assert versions.count(conn, asking["post_id"]) == 1
        assert versions.count(conn, other) == 1

    def test_the_variant_keeps_the_text_and_the_prompts(self, asking):
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        body = post_row(conn, post_id)["body"]
        prompts = [row["prompt"] for row in assets_of(conn, post_id)]

        row = conn.execute(
            "SELECT * FROM post_versions WHERE post_id = ? AND number = 1", (post_id,)
        ).fetchone()

        assert row["body"] == body
        assert [item["prompt"] for item in json.loads(row["assets"])] == prompts

    def test_a_trashed_post_does_not_start_a_new_variant(self, asking):
        """В мусор — значит переделывать нечего, тема уходит другому посту."""
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()

        apply(conn, post_id, Decision.TRASH)

        assert post_row(conn, post_id)["version"] == 1


class TestFilesSurvive:
    def test_each_variant_keeps_its_own_images(self, asking):
        """Общие имена файлов означали, что второй вариант затирает первый."""
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        first = [row["local_path"] for row in assets_of(conn, post_id)]

        apply(conn, post_id, Decision.IMAGES)
        asking["to_review"]()
        second = [row["local_path"] for row in assets_of(conn, post_id)]

        assert set(first) & set(second) == set()
        assert all(Path(path).is_file() for path in first), "картинки первого варианта стёрты"
        assert all(Path(path).is_file() for path in second)

    def test_variants_live_in_separate_folders(self, asking):
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()

        apply(conn, post_id, Decision.SCENES)
        asking["to_review"]()

        assert paths.post_tmp_dir(post_id, 1).is_dir()
        assert paths.post_tmp_dir(post_id, 2).is_dir()


class TestRestoring:
    @pytest.fixture
    def two_variants(self, asking):
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        asking["first_body"] = post_row(conn, post_id)["body"]
        asking["first_images"] = [row["local_path"] for row in assets_of(conn, post_id)]

        apply(conn, post_id, Decision.TEXT)
        asking["to_review"]()
        asking["second_body"] = post_row(conn, post_id)["body"]
        return asking

    def test_the_older_text_comes_back(self, two_variants):
        conn, post_id = two_variants["conn"], two_variants["post_id"]

        assert versions.restore(conn, post_id, 1) is True

        assert post_row(conn, post_id)["body"] == two_variants["first_body"]

    def test_the_older_images_come_back(self, two_variants):
        """Восстановить текст без картинок значит опубликовать не тот пост."""
        conn, post_id = two_variants["conn"], two_variants["post_id"]

        versions.restore(conn, post_id, 1)

        restored = [row["local_path"] for row in assets_of(conn, post_id)]
        assert restored == two_variants["first_images"]
        assert all(Path(path).is_file() for path in restored)

    def test_the_current_number_follows(self, two_variants):
        """Номер нужен верный: по нему кладутся файлы следующей генерации."""
        conn, post_id = two_variants["conn"], two_variants["post_id"]
        versions.restore(conn, post_id, 1)
        assert post_row(conn, post_id)["version"] == 1

        versions.restore(conn, post_id, 2)

        assert post_row(conn, post_id)["version"] == 2

    def test_the_newer_variant_is_not_destroyed(self, two_variants):
        """Вернулись к первому, передумали — второй должен быть на месте."""
        conn, post_id = two_variants["conn"], two_variants["post_id"]
        versions.restore(conn, post_id, 1)

        assert versions.restore(conn, post_id, 2) is True
        assert post_row(conn, post_id)["body"] == two_variants["second_body"]

    def test_a_missing_variant_is_refused(self, two_variants):
        assert versions.restore(two_variants["conn"], two_variants["post_id"], 99) is False

    def test_a_new_variant_after_restoring_does_not_overwrite(self, two_variants):
        """Вернулись к первому, снова нажали «заново» — нужен третий, не второй."""
        conn, post_id = two_variants["conn"], two_variants["post_id"]
        versions.restore(conn, post_id, 1)

        apply(conn, post_id, Decision.TEXT)

        assert post_row(conn, post_id)["version"] == 3


class TestWhatTheOwnerSees:
    def test_a_single_variant_is_not_numbered(self, asking):
        """«Вариант 1 из 1» — лишний шум там, где выбора нет."""
        ctx = asking["to_review"]()

        assert ctx.providers.notifier.sent[0]["total"] == 1

    def test_the_second_variant_is_numbered(self, asking):
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        apply(conn, post_id, Decision.TEXT)

        ctx = asking["to_review"]()

        last = ctx.providers.notifier.sent[-1]
        assert last["version"] == 2
        assert last["total"] == 2


class TestTrashedPostsDoNotPileUp:
    def test_the_files_of_all_variants_are_removed(self, asking):
        """Чистка висела только на публикации, а выброшенный пост не выходит.

        С вариантами это стало заметно: каждый откат оставляет ещё одну папку
        с картинками, и они копились бы на диске вечно.
        """
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        apply(conn, post_id, Decision.IMAGES)
        asking["to_review"]()
        assert paths.post_tmp_dir(post_id, 1).is_dir()
        assert paths.post_tmp_dir(post_id, 2).is_dir()

        apply(conn, post_id, Decision.TRASH)

        assert not paths.post_tmp_dir(post_id).exists()

    def test_a_rollback_keeps_the_files(self, asking):
        """Обратная половина: откат обязан сохранить прежний вариант на диске."""
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()

        apply(conn, post_id, Decision.TEXT)

        assert paths.post_tmp_dir(post_id, 1).is_dir()


class TestRestoringCleansForeignImages:
    def test_extra_images_of_another_variant_are_dropped(self, asking):
        """У вариантов может быть разное число картинок.

        Строки, оставшиеся от другого варианта, иначе уедут в пост чужой
        картинкой — а владелец решал по тем, что видел.
        """
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()

        # У первого варианта одна картинка вместо четырёх.
        with db.write_transaction(conn):
            conn.execute(
                "UPDATE post_versions SET assets = ? WHERE post_id = ? AND number = 1",
                (
                    json.dumps([
                        {"kind": "cover", "position": 0, "prompt": "p", "seed": 1,
                         "local_path": "/тот/самый.png", "external_ref": None}
                    ]),
                    post_id,
                ),
            )

        versions.restore(conn, post_id, 1)

        rows = assets_of(conn, post_id)
        assert rows[0]["local_path"] == "/тот/самый.png"
        assert all(row["local_path"] is None for row in rows[1:]), "остались чужие картинки"


class TestTrashingTheTopic:
    """Выбросить пост вместе с темой — не то же, что выбросить только пост."""

    def test_no_new_variant_is_started(self, asking):
        """Переделывать нечего: тема закрыта, постов по ней больше не будет."""
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        before = post_row(conn, post_id)["version"]

        apply(conn, post_id, Decision.TRASH_TOPIC)

        assert post_row(conn, post_id)["version"] == before

    def test_the_files_are_removed(self, asking):
        conn, post_id = asking["conn"], asking["post_id"]
        asking["to_review"]()
        assert paths.post_tmp_dir(post_id, 1).is_dir()

        apply(conn, post_id, Decision.TRASH_TOPIC)

        assert not paths.post_tmp_dir(post_id).exists()
