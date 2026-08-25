"""Every filesystem path and resource limit, resolved from the environment.

No other module may read ``os.environ`` for these or hardcode a path. Defaults
target a Docker container (``/data``, ``/app/projects``); local development
overrides them, which is why every path expands ``~``.

Values are read on each call rather than cached at import time, so tests can
change the environment between cases.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from factory.core.errors import FactoryError

DEFAULT_DATA_DIR = "/data"
DEFAULT_TMP_DIR = "/tmp/factory"
DEFAULT_PROJECTS_DIR = "/app/projects"

DEFAULT_MAX_PARALLEL_IMAGES = 1
DEFAULT_MAX_STEPS_PER_TICK = 3
DEFAULT_TICK_INTERVAL_SEC = 600
DEFAULT_LOCK_TTL_SEC = 1800

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"", "0", "false", "no", "off"}


def _env_path(name: str, default: str) -> Path:
    raw = os.environ.get(name) or default
    return Path(raw).expanduser()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise FactoryError(
            f"Переменная окружения {name} должна быть целым числом, сейчас там '{raw}'.",
            why="Значение не удалось прочитать как число, поэтому запуск невозможен.",
            what_to_do=(
                f"Исправь строку {name}= в файле {env_file()} "
                f"или убери её совсем — тогда возьмётся значение по умолчанию ({default})."
            ),
        ) from None
    if value < 1:
        raise FactoryError(
            f"Переменная окружения {name} должна быть больше нуля, сейчас там '{raw}'.",
            why="Нулевое или отрицательное значение остановило бы работу воркера.",
            what_to_do=(
                f"Поставь положительное число в {env_file()} "
                f"или убери строку {name}= — тогда возьмётся значение по умолчанию ({default})."
            ),
        )
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    raise FactoryError(
        f"Переменная окружения {name} должна быть 0 или 1, сейчас там '{raw}'.",
        why="Значение не похоже ни на «да», ни на «нет».",
        what_to_do=f"Поставь {name}=0 или {name}=1 в файле {env_file()}.",
    )


def data_dir() -> Path:
    """Database, secrets and backups. The only place state is allowed to live."""
    return _env_path("FACTORY_DATA_DIR", DEFAULT_DATA_DIR)


def tmp_dir() -> Path:
    """Scratch space for generated images. Safe to wipe between runs."""
    return _env_path("FACTORY_TMP_DIR", DEFAULT_TMP_DIR)


def projects_dir() -> Path:
    """Per-project configs, prompts and cover templates."""
    return _env_path("FACTORY_PROJECTS_DIR", DEFAULT_PROJECTS_DIR)


def migrations_dir() -> Path:
    """Numbered ``.sql`` files. Ships next to the package inside the image."""
    override = os.environ.get("FACTORY_MIGRATIONS_DIR")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent.parent / "migrations"


def db_path() -> Path:
    return data_dir() / "factory.db"


def backups_dir() -> Path:
    return data_dir() / "backups"


def env_file() -> Path:
    return data_dir() / ".env"


def post_tmp_dir(post_id: int, version: int | None = None) -> Path:
    """Images of a single post, removed once it is published.

    ``version`` splits variants into their own folders. Without it a second
    generation overwrites the first, and the owner cannot go back to a variant
    they liked more — which is the whole point of having variants.
    """
    root = tmp_dir() / str(post_id)
    return root if version is None else root / f"v{version}"


def max_parallel_images() -> int:
    return _env_int("FACTORY_MAX_PARALLEL_IMAGES", DEFAULT_MAX_PARALLEL_IMAGES)


def max_steps_per_tick() -> int:
    return _env_int("FACTORY_MAX_STEPS_PER_TICK", DEFAULT_MAX_STEPS_PER_TICK)


def tick_interval_sec() -> int:
    return _env_int("FACTORY_TICK_INTERVAL_SEC", DEFAULT_TICK_INTERVAL_SEC)


def lock_ttl_sec() -> int:
    return _env_int("FACTORY_LOCK_TTL_SEC", DEFAULT_LOCK_TTL_SEC)


def ignore_schedule() -> bool:
    """Development-only escape hatch. The tick logs a WARNING while it is on."""
    return _env_bool("FACTORY_IGNORE_SCHEDULE", False)


def ensure_data_dir() -> Path:
    """Create the data directory and prove it is writable.

    The default ``/data`` cannot be created on macOS without ``sudo``, so a bare
    ``PermissionError`` here is the single most likely first-run failure. Turn it
    into an instruction instead.
    """
    target = data_dir()
    # Имя пробного файла уникально на процесс и поток: в docker compose три
    # сервиса стартуют одновременно на одном томе, и общий файл они бы удаляли
    # друг у друга, роняя запуск с «No such file or directory».
    probe = target / f".write-probe-{os.getpid()}-{threading.get_ident()}"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe.touch()
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise FactoryError(
            f"Каталог данных {target} недоступен для записи.",
            why=(
                f"{exc.strerror or exc}. Значение по умолчанию {DEFAULT_DATA_DIR} рассчитано "
                "на запуск в Docker-контейнере; на обычной машине такой каталог "
                "без прав администратора не создаётся."
            ),
            what_to_do=(
                "Укажи свой каталог и повтори команду:\n"
                "  export FACTORY_DATA_DIR=~/factory-data\n"
                "См. README.md → «Установка»."
            ),
        ) from exc
    return target
