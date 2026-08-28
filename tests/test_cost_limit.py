"""Потолок расходов на пост.

Требование SPEC.md, до Этапа 4 не выполнявшееся. Раньше это ничего не значило:
пост стоил 0.14 в валюте провайдера, и упереться в потолок было нечем. Картинки
дороже текста в сорок раз, и предохранитель стал единственным, что стоит между
зациклившимся шагом и счётом.
"""

import pytest

from factory.core import alerts, machine
from factory.core.clock import now_utc, to_iso
from factory.core.config import load_project
from factory.core.logging import get_logger
from factory.core.models import Post, State
from factory.core.retry import record_run
from factory.core.steps import advanced
from factory.providers.registry import build_providers
from tests.conftest import insert_post, insert_project, insert_topic


@pytest.fixture
def post_in_progress(conn, demo_project):
    """Проект demo и один пост в начале цепочки."""
    project = load_project("demo")
    project_id = insert_project(conn, "demo")
    topic_id = insert_topic(conn, project_id, "Тема")
    post_id = insert_post(conn, project_id, topic_id, state=State.QUEUED)
    conn.commit()

    def post() -> Post:
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return Post.from_row(row)

    return {
        "conn": conn,
        "project": project,
        "project_id": project_id,
        "post_id": post_id,
        "post": post,
        "providers": build_providers(project),
    }


def spend(conn, post_id: int, amount: float | None, step: str = "queued") -> None:
    record_run(conn, step=step, ok=True, duration_ms=10, post_id=post_id, cost_usd=amount)


class TestGuard:
    def test_a_post_under_the_limit_goes_on(self, post_in_progress):
        env = post_in_progress
        spend(env["conn"], env["post_id"], 0.10)

        stopped = machine.stop_if_too_expensive(
            env["conn"], env["post"](), env["project"], env["providers"]
        )

        assert stopped is False
        assert env["post"]().state == State.QUEUED

    def test_a_post_over_the_limit_is_stopped(self, post_in_progress):
        env = post_in_progress
        # Потолок проекта demo — 0.40.
        spend(env["conn"], env["post_id"], 0.41)

        stopped = machine.stop_if_too_expensive(
            env["conn"], env["post"](), env["project"], env["providers"]
        )

        assert stopped is True
        assert env["post"]().state == State.FAILED

    def test_exactly_at_the_limit_is_still_allowed(self, post_in_progress):
        """Потолок — это «не дороже», а не «дешевле»."""
        env = post_in_progress
        spend(env["conn"], env["post_id"], 0.40)

        stopped = machine.stop_if_too_expensive(
            env["conn"], env["post"](), env["project"], env["providers"]
        )

        assert stopped is False

    def test_costs_add_up_across_steps(self, post_in_progress):
        env = post_in_progress
        for step in ("queued", "text_ready", "factchecked", "prompts_ready"):
            spend(env["conn"], env["post_id"], 0.15, step=step)

        stopped = machine.stop_if_too_expensive(
            env["conn"], env["post"](), env["project"], env["providers"]
        )

        assert stopped is True

    def test_unknown_prices_do_not_open_a_way_past_the_guard(self, post_in_progress):
        """Часть вызовов без цены не должна отменять предохранитель.

        Ловушка SQLite: ``SUM`` возвращает ``NULL``, если складывать нечего, а
        сравнение с ``NULL`` ложно всегда. Предохранитель, написанный без
        ``COALESCE``, пропускал бы такой пост и выглядел бы работающим.
        """
        env = post_in_progress
        spend(env["conn"], env["post_id"], None, step="queued")
        spend(env["conn"], env["post_id"], 0.50, step="text_ready")
        spend(env["conn"], env["post_id"], None, step="factchecked")

        stopped = machine.stop_if_too_expensive(
            env["conn"], env["post"](), env["project"], env["providers"]
        )

        assert stopped is True

    def test_another_posts_spending_is_not_counted(self, post_in_progress):
        env = post_in_progress
        other = insert_post(
            env["conn"],
            env["project_id"],
            insert_topic(env["conn"], env["project_id"], "Другая"),
            idem_key="demo:other:0",
        )
        spend(env["conn"], other, 5.00)
        env["conn"].commit()

        stopped = machine.stop_if_too_expensive(
            env["conn"], env["post"](), env["project"], env["providers"]
        )

        assert stopped is False


class TestChain:
    def test_the_next_paid_step_never_starts(self, post_in_progress, monkeypatch):
        """Смысл потолка — не сделать следующий платный вызов, а не узнать о нём."""
        env = post_in_progress
        spend(env["conn"], env["post_id"], 1.00)

        calls = []

        def handler(ctx):
            calls.append(ctx.post.id)
            return advanced(State.TEXT_READY)

        monkeypatch.setattr(machine, "handler_for", lambda state: handler)

        done = machine.advance_post(
            env["conn"], env["post"](), env["project"], env["providers"]
        )

        assert calls == []
        assert done == 0

    def test_a_cheap_post_still_moves(self, post_in_progress, monkeypatch):
        env = post_in_progress
        calls = []

        def handler(ctx):
            calls.append(ctx.post.id)
            return advanced(State.TEXT_READY)

        monkeypatch.setattr(machine, "handler_for", lambda state: handler)

        machine.advance_post(
            env["conn"], env["post"](), env["project"], env["providers"], max_steps=1
        )

        assert calls == [env["post_id"]]


class TestAlert:
    def test_the_owner_is_told_with_both_numbers(self, post_in_progress, monkeypatch):
        """«Превышен лимит» без цифр не даёт решить, поднимать потолок или выбросить."""
        env = post_in_progress
        project = _with_telegram(env["project"])
        spend(env["conn"], env["post_id"], 7.25)

        sent: list[str] = []
        providers = env["providers"]
        monkeypatch.setattr(
            providers.notifier, "alert", lambda **kw: sent.append(kw["text"])
        )

        machine.stop_if_too_expensive(env["conn"], env["post"](), project, providers)

        assert len(sent) == 1
        assert "7.25" in sent[0] and "0.40" in sent[0]

    def test_it_is_cleared_when_the_limit_is_raised(self, post_in_progress, monkeypatch):
        """Тревога, не снятая вместе с причиной, при следующем перерасходе промолчит."""
        env = post_in_progress
        project = _with_telegram(env["project"])
        providers = env["providers"]
        monkeypatch.setattr(providers.notifier, "alert", lambda **kw: None)
        spend(env["conn"], env["post_id"], 7.25)
        machine.stop_if_too_expensive(env["conn"], env["post"](), project, providers)

        assert alerts.is_raised(env["conn"], "budget", f"demo:{env['post_id']}")

        # Владелец поднял потолок.
        raised = project.model_copy(
            update={"limits": project.limits.model_copy(update={"max_cost_per_post": 50.0})}
        )
        machine.stop_if_too_expensive(env["conn"], env["post"](), raised, providers)

        assert not alerts.is_raised(env["conn"], "budget", f"demo:{env['post_id']}")


    def test_no_second_alert_with_a_button_that_cannot_work(
        self, post_in_progress, monkeypatch
    ):
        """Общая тревога о сломанном посте предлагает «Попробовать снова».

        Для перерасхода эта кнопка бесполезна: потраченное уже потрачено, и
        повтор упрётся в тот же потолок. Два сообщения об одном и том же, из
        которых второе обещает несбыточное, — ровно то, на что владелец жалуется
        чаще всего.
        """
        env = post_in_progress
        project = _with_telegram(env["project"])
        providers = env["providers"]

        sent: list[dict] = []
        monkeypatch.setattr(providers.notifier, "alert", lambda **kw: sent.append(kw))

        spend(env["conn"], env["post_id"], 7.25)
        machine.stop_if_too_expensive(env["conn"], env["post"](), project, providers)

        machine._alert_failed_posts(
            env["conn"],
            machine.active_projects(env["conn"])[0],
            project,
            providers,
            project.telegram.chat_id,
        )

        assert len(sent) == 1
        assert sent[0].get("fix_post_id") is None


def _with_telegram(project):
    """Копия конфига с адресатом тревог: у demo секции telegram нет."""
    from factory.core.config import TelegramCfg

    return project.model_copy(
        update={"telegram": TelegramCfg(provider="stub", chat_id=1, reviewers=[1])}
    )
