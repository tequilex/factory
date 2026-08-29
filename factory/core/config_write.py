"""Запись настроек проекта обратно в ``config.yaml``.

Самое опасное место панели: сломанный конфиг кладёт проект целиком. Это уже
случалось — на сервер приехал файл, которого не понял код, проект не загрузился,
и вместе с ним пропал список проверяющих. Владелец на «/status» получил «эта
кнопка не для вас», то есть пошёл искать поломку там, где её не было.

Отсюда три правила, и все три проверены тестами:

1. **Сначала проверка, потом запись.** Настройки проверяются тем же валидатором,
   что и при загрузке. Не прошли — на диске остаётся прежний файл, а владелец
   видит ту же человеческую ошибку, что увидел бы воркер.
2. **Комментарии сохраняются.** Половина конфига — пояснения: почему два ключа,
   почему ``supports_reference`` нельзя ставить на глазок, откуда взялся потолок
   в двенадцать рублей. Обычная запись YAML стирает это за один раз.
3. **Запись атомарная.** Временный файл и переименование: обрыв посередине
   оставит либо старую версию, либо новую, но не половину. Воркер читает конфиг
   каждый проход и однажды прочитал бы именно половину.
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from factory.core import paths
from factory.core.config import ProjectConfig, _translate, project_dir
from factory.core.errors import ConfigError
from factory.core.logging import get_logger

log = get_logger(__name__)

#: Поля, которые панель менять не даёт.
#:
#: ``slug`` — имя проекта: оно связано с каталогом и с ключами постов, и его
#: правка через форму означала бы, что половина базы ссылается в никуда.
#: ``root`` — не настройка вовсе, а путь, подставляемый при загрузке.
FROZEN = frozenset({"slug", "root"})


def _yaml() -> YAML:
    editor = YAML()
    editor.preserve_quotes = True
    # Ширина по умолчанию переносит длинные строки посреди слова, и конфиг после
    # первого же сохранения перестаёт читаться глазами.
    editor.width = 4096
    return editor


def _merge(target: Any, changes: dict[str, Any]) -> None:
    """Наложить изменения, не трогая того, чего в них нет.

    Слияние, а не замена: панель присылает один раздел, а в файле их девять.
    Замена целиком означала бы, что правка расписания стирает настройки моделей.
    """
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _dump(document: Any) -> str:
    stream = io.StringIO()
    _yaml().dump(document, stream)
    return stream.getvalue()


def preview(slug: str, changes: dict[str, Any]) -> str:
    """Как будет выглядеть файл после правки. Проверяет, но не пишет.

    Отдельной функцией, чтобы панель могла показать ошибку до нажатия
    «Сохранить», а не после.
    """
    path = project_dir(slug) / "config.yaml"
    if not path.is_file():
        raise ConfigError(
            f"Конфиг проекта '{slug}' не найден: {path}",
            why=f"Каталог с проектами сейчас: {paths.projects_dir()}.",
            what_to_do="Проверь имя проекта или задай FACTORY_PROJECTS_DIR.",
        )

    forbidden = FROZEN & set(changes)
    if forbidden:
        raise ConfigError(
            f"Эти поля менять нельзя: {', '.join(sorted(forbidden))}.",
            why=(
                "Имя проекта связано с каталогом и с ключами постов: его правка "
                "оставит половину базы ссылающейся в никуда."
            ),
            what_to_do="Чтобы завести другую нишу, создайте новый проект.",
        )

    document = _yaml().load(path.read_text(encoding="utf-8"))
    _merge(document, changes)
    text = _dump(document)

    # Проверка ровно та же, что при загрузке: панель не имеет права быть
    # снисходительнее воркера, иначе она примет то, на чём он споткнётся.
    raw = _yaml().load(text)
    try:
        ProjectConfig(**{**raw, "root": project_dir(slug)})
    except ValidationError as exc:
        raise _translate(exc, path) from exc

    return text


def update(slug: str, changes: dict[str, Any]) -> None:
    """Записать изменения в конфиг проекта."""
    text = preview(slug, changes)
    path = project_dir(slug) / "config.yaml"
    _write_atomically(path, text)
    log.info("конфиг проекта изменён", extra={"slug": slug, "fields": sorted(changes)})


def write_text_file(slug: str, relative: str, text: str) -> None:
    """Записать промпт или пример стиля.

    Путь проверяется на выход за пределы проекта: имя файла приходит из запроса,
    и «../../data/.env» иначе перезаписал бы файл секретов правами воркера.
    """
    root = project_dir(slug).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ConfigError(
            "Файл не принадлежит проекту.",
            why=f"Путь {relative} ведёт за пределы каталога проекта.",
            what_to_do="Укажите файл внутри проекта, например prompts/voice.md.",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(target, text)
    log.info("файл проекта изменён", extra={"slug": slug, "file": relative})


def write_bytes_file(slug: str, relative: str, data: bytes) -> None:
    """То же для двоичных файлов: эталонный портрет персонажа."""
    root = project_dir(slug).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ConfigError(
            "Файл не принадлежит проекту.",
            why=f"Путь {relative} ведёт за пределы каталога проекта.",
            what_to_do="Укажите файл внутри проекта, например character/canon.png.",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(target, data)
    log.info("файл проекта заменён", extra={"slug": slug, "file": relative, "bytes": len(data)})


def _write_atomically(path: Path, content: str | bytes) -> None:
    """Временный файл рядом и переименование.

    Рядом, а не в системном каталоге: переименование атомарно только внутри
    одной файловой системы, а на малине данные лежат на отдельном диске.
    """
    mode = "w" if isinstance(content, str) else "wb"
    with tempfile.NamedTemporaryFile(
        mode, encoding="utf-8" if mode == "w" else None,
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)

    try:
        # Права переносятся со старого файла: иначе конфиг после первого
        # сохранения из панели становится доступен всем на чтение.
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
