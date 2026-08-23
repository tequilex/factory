"""Project configuration: the only place a niche is described.

Nothing about a specific topic, persona or group may appear in ``core/`` or
``providers/`` — it all lives in ``projects/<slug>/config.yaml``. Adding a second
group is copying a directory and editing YAML, not writing code.

Validation is strict (``extra="forbid"``) and every failure is translated into a
:class:`ConfigError` naming the file, the field and the fix. The person editing
these files does not read Python tracebacks.
"""

from __future__ import annotations

import os
import re
from datetime import time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from factory.core import paths
from factory.core.errors import ConfigError

# Which provider names a config may name. The implementations live in
# factory/providers/registry.py; a test there asserts the two lists agree.
LLM_PROVIDERS = ("stub", "openai_compatible", "anthropic")
IMAGE_PROVIDERS = ("stub", "replicate")
PUBLISHER_PROVIDERS = ("stub", "vk")

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VkCfg(_Section):
    group_id: int
    token_env: str
    api_version: str = "5.199"
    schedule: list[str] = Field(default_factory=list)
    timezone: str = "Europe/Moscow"
    proxy_env: str | None = None

    @field_validator("schedule")
    @classmethod
    def _check_schedule(cls, value: list[str]) -> list[str]:
        for item in value:
            if not TIME_RE.match(item):
                raise ValueError(
                    f"время '{item}' записано неправильно, нужен формат ЧЧ:ММ, например 19:30"
                )
        return sorted(value)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"часовой пояс '{value}' не найден, нужно название вида Europe/Moscow"
            ) from None
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def slots(self) -> list[time]:
        return [time(int(item[:2]), int(item[3:])) for item in self.schedule]


class PersonaCfg(_Section):
    name: str
    voice_file: str
    style_examples: str


class LlmCfg(_Section):
    provider: Literal[LLM_PROVIDERS]  # type: ignore[valid-type]
    model: str
    factcheck_model: str | None = None
    base_url_env: str | None = None
    api_key_env: str | None = None
    proxy_env: str | None = None


class ImageCfg(_Section):
    provider: Literal[IMAGE_PROVIDERS]  # type: ignore[valid-type]
    model: str
    cover_template: str
    scene_style: str = ""
    lora: str | None = None
    inline_count: int = Field(default=3, ge=0, le=9)
    api_key_env: str | None = None
    proxy_env: str | None = None


class PublisherCfg(_Section):
    provider: Literal[PUBLISHER_PROVIDERS]  # type: ignore[valid-type]


class ContentCfg(_Section):
    post_structure: list[str] = Field(default_factory=lambda: ["hook", "story", "question"])
    target_length: tuple[int, int] = (900, 1400)
    factcheck: Literal["strict", "light", "off"] = "strict"

    @field_validator("target_length")
    @classmethod
    def _check_range(cls, value: tuple[int, int]) -> tuple[int, int]:
        low, high = value
        if low < 1 or high < low:
            raise ValueError(
                f"target_length должен быть [меньшее, большее], сейчас [{low}, {high}]"
            )
        return value


class ReviewCfg(_Section):
    mode: Literal["telegram", "auto"] = "telegram"
    auto_after_n_approved: int = Field(default=40, ge=1)


class LimitsCfg(_Section):
    posts_per_day: int = Field(default=2, ge=1)
    queue_buffer: int = Field(default=6, ge=1)
    max_cost_per_post_usd: float = Field(default=0.40, gt=0)

    @model_validator(mode="after")
    def _buffer_must_cover_a_day(self) -> LimitsCfg:
        if self.queue_buffer < self.posts_per_day:
            raise ValueError(
                f"queue_buffer ({self.queue_buffer}) меньше posts_per_day "
                f"({self.posts_per_day}) — столько постов в сутки система выпустить "
                f"не сможет. Рекомендуемое значение: {self.posts_per_day * 3}"
            )
        return self


class ProjectConfig(_Section):
    slug: str
    vk: VkCfg
    persona: PersonaCfg
    llm: LlmCfg
    image: ImageCfg
    content: ContentCfg
    review: ReviewCfg
    limits: LimitsCfg
    publisher: PublisherCfg = PublisherCfg(provider="vk")

    # Filled in by load_project, not read from YAML.
    root: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _schedule_must_be_able_to_carry_the_daily_limit(self) -> ProjectConfig:
        """Расписание должно вмещать дневной лимит.

        В каждый слот уходит один пост. Пустое расписание или слотов меньше, чем
        posts_per_day, означает посты, которые молча висят в ожидании навсегда:
        это не ошибка выполнения, retry_count не растёт, и заметить это без
        отдельного алерта невозможно. Дешевле отказать при старте.
        """
        if self.publisher.provider == "stub":
            return self

        if not self.vk.schedule:
            raise ValueError(
                "vk.schedule пустое — публиковать будет негде, и одобренные посты "
                "зависнут навсегда. Укажи хотя бы одно время, например [\"19:30\"]"
            )
        if len(self.vk.schedule) < self.limits.posts_per_day:
            raise ValueError(
                f"в vk.schedule {len(self.vk.schedule)} слотов, а posts_per_day = "
                f"{self.limits.posts_per_day}. В каждый слот уходит один пост, поэтому "
                f"нужно минимум {self.limits.posts_per_day} слотов"
            )
        return self

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9_]+", value):
            raise ValueError(
                f"slug '{value}' может содержать только латиницу в нижнем регистре, "
                "цифры и подчёркивание"
            )
        return value

    @property
    def voice_path(self) -> Path:
        return self.root / self.persona.voice_file

    @property
    def examples_dir(self) -> Path:
        return self.root / self.persona.style_examples

    @property
    def cover_template_path(self) -> Path:
        return self.root / self.image.cover_template

    def style_examples(self) -> list[str]:
        """Few-shot examples: every ``.md`` file in the examples directory."""
        return [
            path.read_text(encoding="utf-8")
            for path in sorted(self.examples_dir.glob("*.md"))
        ]

    def voice(self) -> str:
        return self.voice_path.read_text(encoding="utf-8")


_ERROR_PHRASES = {
    "missing": "не хватает обязательного поля",
    "extra_forbidden": "неизвестное поле — скорее всего опечатка в названии",
    "int_type": "должно быть целым числом",
    "int_parsing": "должно быть целым числом",
    "float_type": "должно быть числом",
    "float_parsing": "должно быть числом",
    "string_type": "должно быть строкой",
    "bool_type": "должно быть true или false",
    "list_type": "должно быть списком",
    "dict_type": "должно быть набором полей",
}

# Pydantic пишет пояснения по-английски. Владелец проекта их не читает, поэтому
# каждый тип ошибки переводится явно, а не пробрасывается как есть.
_ERROR_WITH_BOUND = {
    "greater_than": ("gt", "должно быть больше {bound}"),
    "greater_than_equal": ("ge", "должно быть не меньше {bound}"),
    "less_than": ("lt", "должно быть меньше {bound}"),
    "less_than_equal": ("le", "должно быть не больше {bound}"),
    "too_short": ("min_length", "должно содержать хотя бы {bound} элементов"),
    "too_long": ("max_length", "должно содержать не больше {bound} элементов"),
}


def _describe(error: dict[str, Any]) -> str:
    location = ".".join(str(part) for part in error["loc"]) or "(корень файла)"
    kind = error["type"]
    context = error.get("ctx") or {}

    phrase = _ERROR_PHRASES.get(kind)
    if phrase:
        return f"  {location}: {phrase}"

    if kind == "literal_error":
        allowed = str(context.get("expected", "")).replace(" or ", ", ")
        return f"  {location}: допустимые значения — {allowed}"

    if kind in _ERROR_WITH_BOUND:
        key, template = _ERROR_WITH_BOUND[kind]
        if key in context:
            return f"  {location}: {template.format(bound=context[key])}"

    # Ошибки из наших собственных валидаторов уже написаны по-русски.
    message = error["msg"].removeprefix("Value error, ")
    return f"  {location}: {message}"


def _translate(exc: ValidationError, source: Path) -> ConfigError:
    problems = "\n".join(_describe(error) for error in exc.errors())
    return ConfigError(
        f"Конфиг проекта не прошёл проверку: {source}",
        why=f"Найдено проблем: {len(exc.errors())}.\n{problems}",
        what_to_do=(
            "Исправь перечисленные поля в файле и повтори команду. "
            "Образец рабочего конфига — projects/demo/config.yaml. "
            "См. RUNBOOK.md → «Новая группа»."
        ),
    )


def load_env_file(path: Path | None = None) -> int:
    """Load ``KEY=value`` lines from the secrets file into the environment.

    Real environment variables win, so ``docker compose`` (which passes them
    directly) and a local shell both behave the same way. Returns how many
    variables were taken from the file.
    """
    target = path or paths.env_file()
    if not target.is_file():
        return 0

    loaded = 0
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = value.strip().strip('"').strip("'")
            loaded += 1
    return loaded


def resolve_secret(env_name: str, *, context: str) -> str:
    """Read a secret from the environment, or explain exactly what is missing."""
    value = os.environ.get(env_name)
    if not value:
        raise ConfigError(
            f"Не найден секрет для {context}.",
            why=f"Ожидается переменная {env_name}, но она пустая или не задана.",
            what_to_do=(
                f"Добавь строку {env_name}=... в файл {paths.env_file()} "
                "и перезапусти сервис. См. RUNBOOK.md → «Ключи и токены»."
            ),
        )
    return value


def project_dir(slug: str) -> Path:
    return paths.projects_dir() / slug


def available_slugs() -> list[str]:
    """Slugs that have a config on disk. Used by the CLI to suggest alternatives."""
    root = paths.projects_dir()
    if not root.is_dir():
        return []
    return sorted(item.name for item in root.iterdir() if (item / "config.yaml").is_file())


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"Файл {path} не читается как YAML.",
            why=str(exc),
            what_to_do=(
                "Чаще всего дело в отступах или в пропущенном двоеточии. "
                "Сравни с projects/demo/config.yaml."
            ),
        ) from exc

    if raw is None:
        raise ConfigError(
            f"Файл конфига пустой: {path}",
            why="В нём нет ни одной настройки.",
            what_to_do="Скопируй содержимое projects/demo/config.yaml и поправь под свою группу.",
        )
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Файл конфига должен начинаться с набора полей: {path}",
            why=f"Сейчас верхний уровень файла — это {type(raw).__name__}, а не набор настроек.",
            what_to_do="Сравни структуру с projects/demo/config.yaml.",
        )
    return raw


def _check_referenced_files(config: ProjectConfig) -> None:
    """Fail at startup, not three steps into the pipeline."""
    missing: list[str] = []
    if not config.voice_path.is_file():
        missing.append(f"  файл с голосом персонажа: {config.voice_path}")
    if not config.examples_dir.is_dir():
        missing.append(f"  каталог с примерами стиля: {config.examples_dir}")
    if not config.cover_template_path.is_file():
        missing.append(f"  шаблон обложки: {config.cover_template_path}")

    if missing:
        raise ConfigError(
            f"У проекта '{config.slug}' не хватает файлов, указанных в конфиге.",
            why="Не найдено:\n" + "\n".join(missing),
            what_to_do=(
                "Создай недостающие файлы или поправь пути в config.yaml. "
                "См. RUNBOOK.md → «Новая группа»."
            ),
        )


def load_project(slug: str) -> ProjectConfig:
    """Read, validate and sanity-check one project's configuration."""
    root = project_dir(slug)
    path = root / "config.yaml"

    if not path.is_file():
        known = available_slugs()
        hint = f"Найденные проекты: {', '.join(known)}." if known else "Ни одного проекта не найдено."
        raise ConfigError(
            f"Конфиг проекта '{slug}' не найден: {path}",
            why=f"{hint} Каталог с проектами сейчас: {paths.projects_dir()}.",
            what_to_do=(
                "Проверь имя проекта или задай каталог: "
                "export FACTORY_PROJECTS_DIR=/путь/к/projects. "
                "См. RUNBOOK.md → «Новая группа»."
            ),
        )

    raw = _read_yaml(path)
    raw["root"] = root

    try:
        config = ProjectConfig(**raw)
    except ValidationError as exc:
        raise _translate(exc, path) from exc

    if config.slug != slug:
        raise ConfigError(
            f"Имя проекта не совпадает с каталогом: в файле '{config.slug}', каталог '{slug}'.",
            why="Из-за расхождения проект нельзя найти по имени, а idem_key постов разъедется.",
            what_to_do=f"Исправь строку slug: {slug} в файле {path}.",
        )

    _check_referenced_files(config)
    return config
