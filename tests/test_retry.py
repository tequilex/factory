"""Ретраи и учёт вызовов."""

import httpx
import pytest

from factory.core import retry
from factory.core.retry import record_run, tracked_call, with_cost
from tests.conftest import insert_post, insert_project, insert_topic


class Recorder:
    """Собирает паузы вместо того, чтобы спать."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture
def post_id(conn) -> int:
    """Настоящий пост: runs.post_id — внешний ключ, выдумать номер нельзя."""
    project_id = insert_project(conn)
    topic_id = insert_topic(conn, project_id)
    created = insert_post(conn, project_id, topic_id)
    conn.commit()
    return created


def runs(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def status_error(code: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    request = httpx.Request("GET", "https://api.example.com/v1")
    response = httpx.Response(code, headers=headers, request=request)
    return httpx.HTTPStatusError("ошибка", request=request, response=response)


class TestSuccess:
    def test_result_is_returned_unchanged(self, conn):
        @tracked_call("text_ready", conn=conn)
        def work():
            return {"title": "Заголовок"}

        assert work() == {"title": "Заголовок"}

    def test_one_row_is_written(self, conn, post_id):
        @tracked_call("text_ready", conn=conn, post_id=post_id)
        def work():
            return "ok"

        work()

        (row,) = runs(conn)
        assert row["step"] == "text_ready"
        assert row["ok"] == 1
        assert row["post_id"] == post_id
        assert row["error"] is None
        assert row["duration_ms"] >= 0

    def test_cost_is_recorded_when_the_provider_reports_it(self, conn):
        @tracked_call("images_ready", conn=conn)
        def work():
            return with_cost(Draft(), 0.031)

        work()

        assert runs(conn)[0]["cost_usd"] == pytest.approx(0.031)

    def test_cost_is_null_when_nothing_was_reported(self, conn):
        @tracked_call("composed", conn=conn)
        def work():
            return "готово"

        work()

        assert runs(conn)[0]["cost_usd"] is None

    def test_arguments_are_passed_through(self, conn):
        @tracked_call("text_ready", conn=conn)
        def work(a, b, *, c):
            return a + b + c

        assert work(1, 2, c=3) == 6


class TestRetries:
    def test_transient_failure_is_retried_and_succeeds(self, conn):
        sleeper = Recorder()
        attempts = []

        @tracked_call("text_ready", conn=conn, sleep=sleeper)
        def work():
            attempts.append(1)
            if len(attempts) < 3:
                raise httpx.ConnectTimeout("сеть моргнула")
            return "ok"

        assert work() == "ok"
        assert len(attempts) == 3

    def test_a_recovered_call_leaves_exactly_one_successful_row(self, conn):
        """Иначе статистика ошибок раздувается сетевыми морганиями."""
        sleeper = Recorder()
        attempts = []

        @tracked_call("text_ready", conn=conn, sleep=sleeper)
        def work():
            attempts.append(1)
            if len(attempts) < 2:
                raise httpx.ReadTimeout("таймаут")
            return "ok"

        work()

        assert [row["ok"] for row in runs(conn)] == [1]

    def test_backoff_grows_between_attempts(self, conn):
        sleeper = Recorder()

        @tracked_call("text_ready", conn=conn, sleep=sleeper)
        def work():
            raise httpx.ConnectError("недоступен")

        with pytest.raises(httpx.ConnectError):
            work()

        assert sleeper.delays == [1.0, 2.0], "паузы должны расти, а не быть одинаковыми"

    def test_number_of_attempts_matches_the_spec(self, conn):
        """Спека: три захода на сетевые ошибки."""
        sleeper = Recorder()
        attempts = []

        @tracked_call("text_ready", conn=conn, sleep=sleeper)
        def work():
            attempts.append(1)
            raise httpx.ConnectError("недоступен")

        with pytest.raises(httpx.ConnectError):
            work()

        assert len(attempts) == retry.MAX_ATTEMPTS == 3

    @pytest.mark.parametrize("code", sorted(retry.RETRYABLE_STATUS))
    def test_transient_http_codes_are_retried(self, conn, code):
        sleeper = Recorder()
        attempts = []

        @tracked_call("text_ready", conn=conn, sleep=sleeper)
        def work():
            attempts.append(1)
            raise status_error(code)

        with pytest.raises(httpx.HTTPStatusError):
            work()

        assert len(attempts) == 3

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
    def test_permanent_http_codes_are_not_retried(self, conn, code):
        """Повторять запрос с битым ключом три раза — только жечь лимиты."""
        sleeper = Recorder()
        attempts = []

        @tracked_call("text_ready", conn=conn, sleep=sleeper)
        def work():
            attempts.append(1)
            raise status_error(code)

        with pytest.raises(httpx.HTTPStatusError):
            work()

        assert len(attempts) == 1
        assert sleeper.delays == []

    def test_programming_errors_are_not_retried(self, conn):
        """Повторять KeyError бессмысленно: он воспроизведётся точно так же."""
        sleeper = Recorder()
        attempts = []

        @tracked_call("text_ready", conn=conn, sleep=sleeper)
        def work():
            attempts.append(1)
            raise KeyError("title")

        with pytest.raises(KeyError):
            work()

        assert len(attempts) == 1


class TestRetryAfter:
    def test_header_overrides_our_own_backoff(self, conn):
        sleeper = Recorder()

        @tracked_call("text_ready", conn=conn, sleep=sleeper)
        def work():
            raise status_error(429, retry_after="7")

        with pytest.raises(httpx.HTTPStatusError):
            work()

        assert sleeper.delays == [7.0, 7.0]

    def test_absurd_wait_is_capped(self, conn):
        """Провайдер, просящий ждать час, не должен держать блокировку тика."""
        sleeper = Recorder()

        @tracked_call("text_ready", conn=conn, sleep=sleeper)
        def work():
            raise status_error(429, retry_after="3600")

        with pytest.raises(httpx.HTTPStatusError):
            work()

        assert sleeper.delays == [retry.MAX_RETRY_AFTER_SEC] * 2

    @pytest.mark.parametrize("value", ["Wed, 21 Oct 2026 07:28:00 GMT", "soon", "-5"])
    def test_unparsable_header_falls_back_to_our_backoff(self, conn, value):
        sleeper = Recorder()

        @tracked_call("text_ready", conn=conn, sleep=sleeper)
        def work():
            raise status_error(429, retry_after=value)

        with pytest.raises(httpx.HTTPStatusError):
            work()

        assert sleeper.delays == [1.0, 2.0]


class TestFailure:
    def test_exception_reaches_the_caller(self, conn):
        sleeper = Recorder()

        @tracked_call("text_ready", conn=conn, sleep=sleeper)
        def work():
            raise httpx.ConnectError("недоступен")

        with pytest.raises(httpx.ConnectError, match="недоступен"):
            work()

    def test_failure_is_recorded_once_with_the_reason(self, conn, post_id):
        sleeper = Recorder()

        @tracked_call("text_ready", conn=conn, post_id=post_id, sleep=sleeper)
        def work():
            raise httpx.ConnectError("недоступен")

        with pytest.raises(httpx.ConnectError):
            work()

        (row,) = runs(conn)
        assert row["ok"] == 0
        assert row["post_id"] == post_id
        assert "ConnectError" in row["error"]
        assert "недоступен" in row["error"]


class TestContextDiscovery:
    def test_connection_and_post_are_taken_from_the_step_context(self, conn, post_id):
        """Шаги пайплайна получают контекст первым аргументом — дублировать его не нужно."""

        class Ctx:
            def __init__(self, connection):
                self.conn = connection
                self.post = Post(post_id)

        @tracked_call("composed")
        def step(ctx):
            return "ok"

        step(Ctx(conn))

        (row,) = runs(conn)
        assert row["post_id"] == post_id
        assert row["step"] == "composed"

    def test_without_a_connection_nothing_is_recorded_and_nothing_breaks(self):
        @tracked_call("composed")
        def work():
            return "ok"

        assert work() == "ok"


class TestRecordRun:
    def test_rows_accumulate(self, conn):
        record_run(conn, step="text_ready", ok=True, duration_ms=10)
        record_run(conn, step="text_ready", ok=False, duration_ms=20, error="упало")

        assert len(runs(conn)) == 2

    def test_cost_can_be_summed_for_a_post(self, conn):
        """На этом строится и статистика, и лимит стоимости поста."""
        record_run(conn, step="prompts_ready", ok=True, duration_ms=1, cost_usd=0.01)
        record_run(conn, step="images_ready", ok=True, duration_ms=1, cost_usd=0.30)

        total = conn.execute("SELECT SUM(cost_usd) FROM runs").fetchone()[0]
        assert total == pytest.approx(0.31)


class Draft:
    """Мутабельный результат — к нему можно прикрепить стоимость."""


class Post:
    def __init__(self, post_id: int) -> None:
        self.id = post_id
