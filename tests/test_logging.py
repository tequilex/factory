"""Логи — JSON в stdout. Секреты не логируются никогда."""

import io
import json
import logging

import pytest

from factory.core.logging import get_logger, setup_logging


class LogCapture:
    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    @property
    def raw(self) -> str:
        return self._stream.getvalue()

    def entries(self) -> list[dict]:
        return [json.loads(line) for line in self.raw.splitlines() if line.strip()]

    def only(self) -> dict:
        entries = self.entries()
        assert len(entries) == 1, f"ожидалась одна строка лога, получено {len(entries)}"
        return entries[0]


@pytest.fixture
def captured():
    root = logging.getLogger()
    previous_level = root.level
    stream = io.StringIO()
    setup_logging(level="DEBUG", stream=stream)
    yield LogCapture(stream)
    # Уровень тоже надо вернуть: иначе следующие тесты пойдут с DEBUG и поведение
    # логирования в них будет отличаться от боевого.
    root.handlers.clear()
    root.setLevel(previous_level)


def test_line_is_valid_json_with_required_fields(captured):
    get_logger("test").info("тик начался")

    entry = captured.only()
    assert entry["level"] == "INFO"
    assert entry["msg"] == "тик начался"
    assert entry["logger"] == "test"
    assert entry["ts"].endswith("Z")


def test_extra_fields_land_in_the_json(captured):
    get_logger("test").info("пост продвинулся", extra={"post_id": 7, "state": "composed"})

    entry = captured.only()
    assert entry["post_id"] == 7
    assert entry["state"] == "composed"


def test_cyrillic_is_written_readable_not_escaped(captured):
    r"""тик вместо «тик» делает логи бесполезными для владельца."""
    get_logger("test").info("темы закончились")

    assert "темы закончились" in captured.raw


def test_exception_is_recorded(captured):
    log = get_logger("test")
    try:
        raise ValueError("сломалось")
    except ValueError:
        log.exception("шаг упал")

    entry = captured.only()
    assert entry["msg"] == "шаг упал"
    assert "ValueError: сломалось" in entry["error"]


@pytest.mark.parametrize(
    "field",
    ["vk_token", "api_key", "LLM_API_KEY", "client_secret", "password", "authorization", "key"],
)
def test_secret_looking_fields_are_redacted(captured, field):
    get_logger("test").info("вызов", extra={field: "s3cr3t-value"})

    entry = captured.only()
    assert entry[field] == "***"
    assert "s3cr3t-value" not in captured.raw


def test_nested_secrets_are_redacted(captured):
    get_logger("test").info("вызов", extra={"cfg": {"model": "gpt-4o", "api_key": "abc123xyz"}})

    entry = captured.only()
    assert entry["cfg"]["api_key"] == "***"
    assert entry["cfg"]["model"] == "gpt-4o"


def test_secrets_inside_lists_are_redacted(captured):
    get_logger("test").info("вызов", extra={"calls": [{"token": "abc123xyz"}]})

    assert "abc123xyz" not in captured.raw


def test_idem_key_is_not_mistaken_for_a_secret(captured):
    """idem_key — не секрет, а рабочий идентификатор; затирать его нельзя."""
    get_logger("test").info("пост создан", extra={"idem_key": "demo:12:0"})

    assert captured.only()["idem_key"] == "demo:12:0"


def test_secret_env_values_are_scrubbed_from_the_message(captured, monkeypatch):
    """Страховка от log.info(f'token={t}') — фильтр по именам полей такое пропустит."""
    monkeypatch.setenv("VK_TOKEN_DEMO", "vk1-super-secret-token")
    get_logger("test").info("публикую с токеном vk1-super-secret-token")

    entry = captured.only()
    assert "vk1-super-secret-token" not in captured.raw
    assert "***" in entry["msg"]


def test_short_env_values_are_not_scrubbed(captured, monkeypatch):
    """Короткое значение секретом не бывает, зато вычистило бы обычные слова."""
    monkeypatch.setenv("LLM_API_KEY", "no")
    get_logger("test").info("сообщение со словом no внутри")

    assert captured.only()["msg"] == "сообщение со словом no внутри"


def test_non_secret_env_values_are_left_alone(captured, monkeypatch):
    monkeypatch.setenv("FACTORY_DATA_DIR", "/srv/factory-data")
    get_logger("test").info("данные в /srv/factory-data")

    assert captured.only()["msg"] == "данные в /srv/factory-data"


@pytest.mark.parametrize("field", ["created", "module", "name", "filename", "args", "message"])
def test_field_names_that_clash_with_logrecord_do_not_crash(captured, field):
    """logging падает с KeyError, если extra называется как поле LogRecord.

    Ловушка в том, что падение происходит только когда уровень логирования
    действительно включён: в разработке молчит, в бою роняет воркер. Имена
    «created», «module», «name» — ровно те, что просятся сами.
    """
    get_logger("test").info("тик завершён", extra={field: 42})

    entry = captured.only()
    assert entry[f"f_{field}"] == 42
    assert entry["msg"] == "тик завершён"


def test_non_clashing_fields_keep_their_names(captured):
    get_logger("test").info("пост создан", extra={"post_id": 7, "created_at": "вчера"})

    entry = captured.only()
    assert entry["post_id"] == 7
    assert entry["created_at"] == "вчера"


def test_setup_is_idempotent():
    """Повторный вызов не должен удваивать строки: воркер и CLI оба его зовут."""
    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)
    setup_logging(level="INFO", stream=stream)
    try:
        get_logger("test").info("один раз")
        assert len(stream.getvalue().strip().splitlines()) == 1
    finally:
        logging.getLogger().handlers.clear()
