"""Ошибки — тоже документация: владелец проекта не читает код."""

import pytest

from factory.core.errors import ConfigError, DbError, FactoryError, LockError, ProviderError


def test_full_message_has_three_parts_in_order():
    err = FactoryError(
        "Не найден токен VK для проекта demo.",
        why="Ожидается переменная VK_TOKEN_DEMO в /data/.env.",
        what_to_do="См. RUNBOOK.md → «Ключи и токены».",
    )
    lines = str(err).splitlines()

    assert len(lines) == 3
    assert lines[0] == "Не найден токен VK для проекта demo."
    assert lines[1] == "Причина: Ожидается переменная VK_TOKEN_DEMO в /data/.env."
    assert lines[2] == "Что делать: См. RUNBOOK.md → «Ключи и токены»."


def test_optional_parts_are_omitted():
    assert str(FactoryError("Просто сломалось.")) == "Просто сломалось."
    assert str(FactoryError("Сломалось.", why="Потому что.")) == "Сломалось.\nПричина: Потому что."


def test_parts_are_available_separately():
    err = FactoryError("что", why="почему", what_to_do="что делать")
    assert err.what == "что"
    assert err.why == "почему"
    assert err.what_to_do == "что делать"


@pytest.mark.parametrize("cls", [ConfigError, DbError, ProviderError, LockError])
def test_subclasses_are_catchable_as_factory_error(cls):
    with pytest.raises(FactoryError):
        raise cls("сломалось", why="почему", what_to_do="почини")


def test_is_an_exception():
    assert issubclass(FactoryError, Exception)
