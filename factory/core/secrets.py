"""Правка файла секретов на ходу.

Существует ради одной беды: ключ загрузки картинок в ВК живёт 24 часа, продлить
его нельзя, а без него не публикуется ни один пост. Раз в сутки нужен человек с
руками — и единственное, что можно сделать, это чтобы руки требовались на
пятнадцать секунд в телефоне, а не на заход по ssh с ноутбука.

Отсюда два требования, которых нет у обычного чтения конфига:

* **записать значение в файл, не потеряв остальные.** Файл правит и человек, и
  система; переписывание целиком через временный файл с переименованием — то,
  что переживёт выключение питания посередине;
* **подхватить новое значение без перезапуска.** Иначе владелец вставляет ключ,
  а публикация всё равно ждёт до утра — ровно та беспомощность, которую всё это
  и должно убрать.

Значения секретов не логируются никогда. В сообщениях — только имена.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from factory.core import paths
from factory.core.errors import ConfigError
from factory.core.logging import get_logger

log = get_logger(__name__)

#: Имена, которые система положила в окружение из файла. Только их она вправе
#: обновлять при перечитывании: то, что пришло настоящей переменной окружения
#: (docker compose, шелл), задано снаружи и главнее файла.
_FROM_FILE: set[str] = set()

# Владелец читает и пишет, остальные — никак. Файл с ключами от сообщества и
# от платных моделей не должен быть доступен другим пользователям машины.
_PRIVATE = stat.S_IRUSR | stat.S_IWUSR


def _parse(text: str) -> tuple[dict[str, str], list[str]]:
    """Разобрать содержимое файла. Возвращает значения и имена-дубликаты."""
    values: dict[str, str] = {}
    duplicates: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name:
            continue
        if name in values:
            duplicates.append(name)
        values[name] = value.strip().strip('"').strip("'")
    return values, duplicates


def load_env_file(path: Path | None = None, *, refresh: bool = False) -> int:
    """Прочитать ``KEY=value`` из файла секретов в окружение.

    Два правила, оба выведены из того, как файл правят на самом деле:

    * **побеждает последняя строка.** RUNBOOK учит дописывать ключи через
      ``>>``, поэтому повторный запуск оставляет две строки с одним именем.
      Взять первую значило бы молча сохранить устаревшее значение — именно так
      однажды заглушка победила настоящий ключ и стоила часа разбирательств;
    * **настоящая переменная окружения главнее файла**, чтобы ``docker compose``
      и обычный шелл вели себя одинаково.

    ``refresh=True`` разрешает обновить те значения, которые система сама же и
    взяла из файла. Это нужно воркеру: ключ ВК, вставленный владельцем через
    бота, обязан подхватиться на следующем тике, а не после перезапуска.
    Значения, пришедшие снаружи, не трогаются и в этом режиме.

    Возвращает, сколько переменных взято из файла.
    """
    target = path or paths.env_file()
    if not target.is_file():
        return 0

    values, duplicates = _parse(target.read_text(encoding="utf-8"))

    if duplicates:
        log.warning(
            "в файле секретов есть повторяющиеся строки, взято последнее значение",
            extra={"file": str(target), "names": sorted(set(duplicates))},
        )

    loaded = 0
    for name, value in values.items():
        ours = name in _FROM_FILE
        if name in os.environ and not (refresh and ours):
            continue
        if os.environ.get(name) != value:
            os.environ[name] = value
        _FROM_FILE.add(name)
        loaded += 1
    return loaded


def update_secret(name: str, value: str, path: Path | None = None) -> None:
    """Записать значение в файл секретов и применить его немедленно.

    Файл переписывается целиком через временный с переименованием: обрыв
    посередине оставит либо старую версию, либо новую, но не половину.
    Повторы одного имени схлопываются в одну строку — иначе файл разрастается,
    а разбираться в нём приходится человеку.
    """
    if not name or "=" in name or "\n" in name:
        raise ConfigError(
            "Недопустимое имя секрета.",
            why=f"Имя {name!r} содержит '=' или перенос строки.",
            what_to_do="Имя переменной — латинские буквы, цифры и подчёркивание.",
        )
    if "\n" in value:
        raise ConfigError(
            f"Значение {name} содержит перенос строки.",
            why="Строка файла секретов — одна пара имя=значение.",
            what_to_do="Скопируй значение заново, без переносов.",
        )

    target = path or paths.env_file()
    target.parent.mkdir(parents=True, exist_ok=True)

    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    values, _ = _parse(existing)
    replaced = name in values
    values[name] = value

    body = "".join(f"{key}={item}\n" for key, item in values.items())

    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, prefix=".env.", delete=False
    )
    try:
        with handle:
            handle.write(body)
        os.chmod(handle.name, _PRIVATE)
        os.replace(handle.name, target)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise

    # Значение пришло от системы, а не снаружи: дальше его можно обновлять.
    os.environ[name] = value
    _FROM_FILE.add(name)

    log.info(
        "секрет обновлён",
        extra={"name": name, "file": str(target), "f_replaced": replaced},
    )
