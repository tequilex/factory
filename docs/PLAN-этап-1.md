# Этап 1: Скелет — план реализации

> **Для агентов-исполнителей:** выполнять задачи по порядку, по одной. Каждый шаг — чекбокс.
> Тесты пишутся ДО реализации. Коммит после каждой задачи.

**Цель:** рабочий каркас контент-фабрики: база, конфиги, стейт-машина, CLI. Все внешние
сервисы (LLM, картинки, ВК) — заглушки. Пост проезжает весь путь `queued → published`,
процесс переживает `kill -9`, дублей не возникает.

**Архитектура:** пост — конечный автомат в SQLite. Воркер по таймеру берёт посты в
нетерминальных состояниях и продвигает каждый на несколько шагов (`FACTORY_MAX_STEPS_PER_TICK`),
фиксируя состояние в базе после КАЖДОГО шага. Шаги вызывают провайдеров через три протокола;
на этом этапе подключены stub-реализации, дальше меняется только строка в конфиге.

**Стек:** Python 3.11, SQLite (WAL), pydantic v2, PyYAML, httpx, Pillow, typer, pytest, uv.

---

## Решения, зафиксированные до начала работы

| # | Решение | Обоснование |
|---|---------|-------------|
| 1 | `posts_per_day` считается по **опубликованным** за сегодня | иначе очередь разъезжается с расписанием при отложенном ревью |
| 2 | `queue_buffer` (по умолчанию 6) — сколько постов держать в работе одновременно. Считаются все посты в нетерминальных состояниях, от `queued` до `approved`. Правило подбора: `queue_buffer ≈ posts_per_day × 3` | развязывает наполнение очереди и публикацию; трёхдневный запас переживает выходные без ревью |
| 3 | `FACTORY_MAX_STEPS_PER_TICK` (по умолчанию 3) — сколько шагов пост проезжает за тик | решает «80 минут на пост» в корне, а не обходным путём |
| 4 | Состояние коммитится в базу после каждого шага, даже внутри цепочки | краш в середине цепочки не теряет прогресс предыдущих шагов |
| 5 | Тема занимается в момент **создания** поста, а не в шаге `text` | `posts.topic_id` обязателен, `idem_key = slug:topic_id:attempt` |
| 6 | Все пути и лимиты — только через `core/paths.py` | ноль хардкодов, переезд на другое железо без правок кода |
| 7 | Время в базе — UTC ISO-8601 (`2026-08-23T10:15:00Z`); расписание и отчёты — в часовом поясе проекта | переезд сервера не ломает расписание |
| 8 | CLI на `typer` | понятный `--help` и внятные ошибки важнее экономии зависимости |
| 9 | Ошибки и документация по-русски; имена в коде и комментарии — по-английски | русские идентификаторы — источник багов |
| 10 | Файлы картинок — только в `FACTORY_TMP_DIR`, `/dev/shm` из спеки игнорируем | в спеке противоречие, `FACTORY_TMP_DIR` покрывает оба случая |
| 11 | Шрифты — в `factory/compose/fonts/` | в спеке два разных места, берём то, что в дереве репозитория |

### Добавления к спеке (чего в ней нет, но без чего этап не собирается)

| Что | Зачем |
|-----|-------|
| Таблица `meta (key, value, updated_at)` | блокировка тика и хартбит; в схеме спеки для них нет места |
| Колонки `posts.factcheck_verdict`, `posts.factcheck_notes` | спека требует показывать «⚠️ фактчек не уверен», но хранить вердикт негде |
| Колонка `posts.published_at` | дневной лимит нельзя считать по `updated_at`: он меняется при любой правке строки. Пост, опубликованный вчера в 23:50, съел бы сегодняшний слот, если сегодня его строку тронул `post retry` или воркер комментариев с Этапа 6. `published_at` пишется ровно один раз — в момент публикации |
| `core/clock.py` — `now_utc()` | тестам нужно управлять временем без внешних библиотек |
| `core/errors.py` — `FactoryError(what, why, what_to_do)` | спека требует, чтобы каждая ошибка была инструкцией |
| `core/lock.py` | блокировка тика с TTL и перехватом протухшей |
| Env `FACTORY_LOCK_TTL_SEC` (1800) | после `kill -9` блокировка не должна висеть дольше, чем нужно |
| Env `FACTORY_IGNORE_SCHEDULE` (`0`) | только для разработки: пропустить проверку расписания, иначе сквозной прогон невозможен. **При значении `1` тик пишет WARNING в лог на каждой итерации** — иначе переменная однажды уедет в боевой конфиг |

Всё перечисленное уже внесено в `SPEC.md` — план и спека не расходятся.

### Задолженность Этапа 5 (сейчас НЕ реализуется, только фиксируется)

Две дыры, которые видны уже сейчас, но закрываются только когда появится Telegram-бот.
Обе должны попасть в `CLAUDE.md` в задаче 16, иначе о них забудут.

| Что | Почему нельзя оставить как есть |
|-----|--------------------------------|
| **Алерт «нечем публиковать»** | Срабатывает на реальный симптом: свободных тем не осталось, ИЛИ ближайший слот расписания нечем закрыть. НЕ на «N постов ждут ревью» — это нормальная рабочая ситуация, и алерт на неё превратится в шум, который перестанут читать |
| **Мягкий таймаут на `WAITING`** | `WAITING` намеренно не увеличивает `retry_count`, поэтому пост может висеть в одном состоянии бесконечно и никто этого не заметит. Нужно: висит дольше суток → уведомление владельцу. Не переводить в `failed` — ожидание человека это не ошибка |

---

## Карта файлов

| Файл | Ответственность |
|------|-----------------|
| `pyproject.toml` | зависимости, команда `factory`, настройки pytest |
| `.python-version` | `3.11` |
| `.gitignore`, `.env.example` | служебное |
| `factory/core/paths.py` | все пути и числовые лимиты из env со значениями по умолчанию; понятная ошибка, если каталог данных недоступен на запись |
| `factory/core/clock.py` | `now_utc()`, `to_iso()`, `from_iso()` — единая точка времени |
| `factory/core/errors.py` | `FactoryError` и наследники с человекочитаемым текстом |
| `factory/core/logging.py` | JSON-логи в stdout, затирание секретов |
| `factory/core/db.py` | соединение SQLite (WAL, busy_timeout), применение миграций, транзакции |
| `factory/core/models.py` | `State`, `Post`, `Topic`, `Asset`, `Run`, `Rejection` (dataclasses) |
| `factory/core/config.py` | pydantic-модели конфига проекта, загрузка YAML, разрешение секретов из env |
| `factory/core/lock.py` | блокировка тика с TTL, хартбит |
| `factory/core/http.py` | фабрика httpx-клиентов: прокси на провайдера, таймауты |
| `factory/core/retry.py` | `@tracked_call(step)`: 3 сетевые попытки, 429 + `Retry-After`, запись в `runs` |
| `factory/core/machine.py` | тик: блокировка → пополнение очереди → продвижение постов → хартбит |
| `factory/core/steps/__init__.py` | контракт шага (`StepResult`), реестр `состояние → обработчик` |
| `factory/core/steps/text.py` | `queued → text_ready` |
| `factory/core/steps/factcheck.py` | `text_ready → factchecked` |
| `factory/core/steps/prompts.py` | `factchecked → prompts_ready` |
| `factory/core/steps/images.py` | `prompts_ready → images_ready` |
| `factory/core/steps/compose.py` | `images_ready → composed` |
| `factory/core/steps/review.py` | `composed → in_review` и `in_review → approved` |
| `factory/core/steps/publish.py` | `approved → published` (расписание + дневной лимит) |
| `factory/providers/base.py` | три протокола: `LLMProvider`, `ImageProvider`, `Publisher` |
| `factory/providers/registry.py` | выбор реализации по строке из конфига |
| `factory/providers/llm/stub.py` | детерминированный фейковый текст |
| `factory/providers/images/stub.py` | PNG-заглушка через Pillow |
| `factory/providers/publishers/stub.py` | пишет файл + лог, возвращает фейковый id |
| `factory/workers/tick.py` | точка входа воркера: `--once` / `--loop` |
| `factory/cli.py` | команды `typer` |
| `migrations/001_init.sql` | схема из спеки + `meta` + колонки фактчека |
| `projects/demo/config.yaml` | учебный проект, все провайдеры `stub` |
| `projects/demo/prompts/voice.md`, `prompts/examples/*.md` | заглушки |
| `projects/demo/templates/red_frame.json` | шаблон обложки (используется на Этапе 2) |
| `tests/*` | см. задачи ниже |
| `README.md`, `RUNBOOK.md`, `TROUBLESHOOTING.md`, `CLAUDE.md` | документация этапа |

Не создаётся на Этапе 1: `bot/`, `workers/comments.py`, `compose/cover.py`, `deploy/`, `.github/`.

---

## Ключевые контракты

Эти куски задают форму всего остального — они приведены дословно, чтобы не разъезжались
между задачами.

### Состояния

```python
TERMINAL = {"published", "failed", "rejected"}

TRANSITIONS = {
    "queued":        "text_ready",
    "text_ready":    "factchecked",
    "factchecked":   "prompts_ready",
    "prompts_ready": "images_ready",
    "images_ready":  "composed",
    "composed":      "in_review",
    "in_review":     "approved",
    "approved":      "published",
}
```

### Контракт шага

```python
class Outcome(str, Enum):
    ADVANCED = "advanced"   # шаг выполнен, состояние сменилось
    WAITING  = "waiting"    # ждём внешнего события (человека, расписания) — это НЕ ошибка

@dataclass(frozen=True)
class StepResult:
    outcome: Outcome
    next_state: str | None = None
    reason: str | None = None      # для WAITING — что именно ждём, попадает в лог

@dataclass
class StepContext:
    conn: sqlite3.Connection
    project: ProjectConfig
    post: Post
    providers: Providers
    log: Logger

Handler = Callable[[StepContext], StepResult]
REGISTRY: dict[str, Handler]      # состояние → обработчик
```

Разница `WAITING` и ошибки принципиальна: `WAITING` не увеличивает `retry_count`
и не ведёт к `failed`. Пост в `in_review`, ждущий человека неделю, не должен «сгореть».

### Продвижение поста

```
advance_post(post, max_steps):
    for _ in range(max_steps):
        handler = REGISTRY[post.state]
        try:
            result = tracked_call(post.state)(handler)(ctx)
        except Exception as exc:
            record_failure(post, exc)     # retry_count += 1, last_error, next_attempt_at
            return                        # цепочка обрывается, ждём следующего тика
        if result.outcome is WAITING:
            record_wait(post, result.reason)
            return
        commit_transition(post, result.next_state)   # ← COMMIT здесь, отдельной транзакцией
        post = reload(post)
        if post.state in TERMINAL:
            return
```

Отдельный коммит на каждый переход — это ровно то, что делает краш в середине цепочки
безопасным.

### Backoff

```python
delay_sec = min(600 * 2 ** (retry_count - 1), 21600)   # потолок 6 ч
# retry_count >= 5 → state = "failed"
```

На практике применяются только четыре паузы — 10, 20, 40, 80 минут. Пятый отказ
переводит пост в `failed`, поэтому ни 160 минут, ни потолок в 6 часов при пяти
попытках не достигаются. Потолок оставлен на случай, если порог когда-нибудь поднимут.

### Пополнение очереди и дневной лимит

```
replenish_queue(project):
    in_flight = COUNT posts WHERE project_id = ? AND state NOT IN ('published','failed','rejected')
    while in_flight < project.limits.queue_buffer:
        topic = claim_free_topic(project)      # атомарный UPDATE ... RETURNING
        if topic is None: log("темы закончились"); break
        create_post(project, topic)            # idem_key = f"{slug}:{topic.id}:{attempt}"
        in_flight += 1
```

`attempt` — число прошлых отклонений этой темы (`COUNT` по `rejections` через
её посты). Для темы, взятой в работу впервые, это `0`.

```
publish step:
    if post.external_id is not None: return ADVANCED   # уже опубликован, идемпотентность
    published_today = COUNT posts WHERE project_id = ? AND published_at IS NOT NULL
                                    AND date(published_at в tz проекта) = сегодня
    if published_today >= limits.posts_per_day: return WAITING("дневной лимит исчерпан")
    if not schedule_slot_open(project) and not FACTORY_IGNORE_SCHEDULE:
        return WAITING("вне расписания публикаций")
    external_id = publisher.publish(post, assets)
    ...
```

### Конфиг проекта (`projects/demo/config.yaml`)

```yaml
slug: demo

vk:
  group_id: 0
  token_env: VK_TOKEN_DEMO
  api_version: "5.199"
  schedule: ["19:30", "21:00"]
  timezone: "Europe/Moscow"

persona:
  name: "Кристина"
  voice_file: prompts/voice.md
  style_examples: prompts/examples/

llm:
  provider: stub
  model: "stub"
  factcheck_model: "stub"

image:
  provider: stub
  model: "stub"
  inline_count: 3
  cover_template: templates/red_frame.json
  scene_style: "cinematic photo, overcast light, 35mm"

publisher:
  provider: stub

content:
  post_structure: [hook, story, detail, question]
  target_length: [900, 1400]
  factcheck: strict

review:
  mode: auto
  auto_after_n_approved: 40

limits:
  posts_per_day: 2           # сколько ПУБЛИКУЕТСЯ в сутки
  queue_buffer: 6            # сколько постов держать в работе; правило: posts_per_day × 3
  max_cost_per_post_usd: 0.40
```

---

## Задачи

### Задача 0: репозиторий и окружение

**Файлы:** `pyproject.toml`, `.python-version`, `.gitignore`, `.env.example`,
`factory/__init__.py`, `tests/__init__.py`

- [x] **Шаг 1.** Установить `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [x] **Шаг 2.** Создать `pyproject.toml`: зависимости `httpx`, `pydantic>=2`, `pyyaml`, `pillow`, `typer`; dev-группа `pytest`; `[project.scripts] factory = "factory.cli:app"`; секция `[tool.pytest.ini_options]` с `testpaths = ["tests"]`
- [x] **Шаг 3.** `.gitignore`: `.venv/`, `__pycache__/`, `*.db`, `*.db-wal`, `*.db-shm`, `.env`, `data/`, `tmp/`
- [x] **Шаг 4.** `uv sync` — проверить, что виртуальное окружение создалось
- [x] **Шаг 5.** Прогнать `uv run pytest` — ожидаемо «no tests ran», без ошибок импорта
- [x] **Шаг 6.** Коммит: `chore: каркас проекта и зависимости`

---

### Задача 1: пути, время, ошибки, логи

**Файлы:** создать `factory/core/paths.py`, `factory/core/clock.py`, `factory/core/errors.py`,
`factory/core/logging.py`; тесты `tests/test_paths.py`, `tests/test_errors.py`, `tests/test_logging.py`

- [x] **Шаг 1.** Тест `tests/test_paths.py`: без env — значения по умолчанию (`/data`, `/tmp/factory`, `/app/projects`, `1`, `600`, `3`, `1800`); с выставленными env — берутся они; `post_tmp_dir(42)` возвращает `{FACTORY_TMP_DIR}/42`
- [x] **Шаг 1а.** Тест: `ensure_data_dir()` при недоступном на запись каталоге (подставить `/data` под обычным пользователем macOS) бросает `FactoryError`, а не `PermissionError`. В тексте должно быть: каталог, причина и готовая к копипасту подсказка `export FACTORY_DATA_DIR=~/factory-data`
- [x] **Шаг 2.** Запустить, убедиться что падает
- [x] **Шаг 3.** Реализовать `paths.py`: функции `data_dir()`, `tmp_dir()`, `projects_dir()`, `db_path()`, `backups_dir()`, `env_file()`, `post_tmp_dir(post_id)`, `max_parallel_images()`, `tick_interval_sec()`, `max_steps_per_tick()`, `lock_ttl_sec()`, `ignore_schedule()`, `ensure_data_dir()`. Числа читаются через хелпер, который на нечисловое значение бросает `FactoryError` с текстом вида «Переменная FACTORY_TICK_INTERVAL_SEC должна быть числом, сейчас там 'abc'. Исправь /data/.env». `ensure_data_dir()` ловит `PermissionError`/`OSError` при создании каталога и переводит в `FactoryError` с подсказкой про `FACTORY_DATA_DIR` — значение по умолчанию `/data` рассчитано на контейнер и на macOS без `sudo` не создаётся
- [x] **Шаг 4.** Реализовать `clock.py`: `now_utc() -> datetime` (aware, UTC), `to_iso(dt) -> str`, `from_iso(s) -> datetime`
- [x] **Шаг 5.** Тест `tests/test_errors.py`: `FactoryError("что", why="почему", what_to_do="что делать")` при печати даёт три строки в этом порядке
- [x] **Шаг 6.** Реализовать `errors.py`: `FactoryError` + `ConfigError`, `DbError`, `ProviderError`, `LockError`
- [x] **Шаг 7.** Тест `tests/test_logging.py`: строка лога — валидный JSON с полями `ts`, `level`, `msg`; значение с ключом, содержащим `token`/`key`/`secret`/`password`, заменено на `***`
- [x] **Шаг 8.** Реализовать `logging.py`: `setup_logging()`, JSON-форматтер, фильтр секретов
- [x] **Шаг 9.** `uv run pytest tests/test_paths.py tests/test_errors.py tests/test_logging.py -v` — всё зелёное
- [x] **Шаг 10.** Коммит: `feat: пути, время, ошибки и структурные логи`

---

### Задача 2: миграции и подключение к базе

**Файлы:** создать `migrations/001_init.sql`, `factory/core/db.py`; тест `tests/test_migrations.py`,
фикстуры `tests/conftest.py`

- [x] **Шаг 1.** `tests/conftest.py`: фикстура `tmp_env` — подменяет `FACTORY_DATA_DIR`/`FACTORY_TMP_DIR`/`FACTORY_PROJECTS_DIR` на временные каталоги; фикстура `conn` — открытая база с применёнными миграциями; **автоиспользуемая (`autouse=True`) фикстура `no_network`** — подменяет `httpx.Client`/`httpx.AsyncClient` на транспорт, который на любой реальный запрос бросает `RuntimeError("Тест попытался сходить в сеть: {method} {url}. Внешние вызовы в тестах должны быть замоканы.")`. Тесты, которым нужен настоящий транспорт (их не будет на этом этапе), отключают её маркером
- [x] **Шаг 1а.** Тест `tests/test_no_network.py`: попытка `httpx.get("https://example.com")` внутри теста падает с этим сообщением. Это тест на сам предохранитель — без него фикстура может тихо перестать работать
- [x] **Шаг 2.** Тест `tests/test_migrations.py`:
  - после `migrate()` существуют таблицы `projects, topics, posts, assets, comments, runs, rejections, meta`
  - `PRAGMA user_version` == 1
  - повторный `migrate()` не падает и ничего не меняет
  - `PRAGMA journal_mode` == `wal`
  - вставка двух постов с одинаковым `idem_key` даёт `IntegrityError`
- [x] **Шаг 3.** Запустить, убедиться что падает
- [x] **Шаг 4.** Написать `migrations/001_init.sql`: вся схема из спеки, плюс `meta(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)`, плюс колонки `posts.factcheck_verdict TEXT`, `posts.factcheck_notes TEXT`, `posts.published_at TEXT`, плюс индексы из спеки и `CREATE INDEX idx_posts_published ON posts(project_id, published_at)`
- [x] **Шаг 5.** Реализовать `db.py`: `connect()` (создаёт каталог, `PRAGMA journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`, `row_factory=sqlite3.Row`), `migrate(conn)` (читает `PRAGMA user_version`, применяет по порядку файлы `NNN_*.sql` с номером выше текущего, каждый в транзакции, обновляет `user_version`), контекст-менеджер `transaction(conn)`
- [x] **Шаг 6.** `uv run pytest tests/test_migrations.py -v` — зелёное
- [x] **Шаг 7.** Коммит: `feat: схема базы и применение миграций`

---

### Задача 3: модели данных

**Файлы:** создать `factory/core/models.py`; тест `tests/test_models.py`

- [x] **Шаг 1.** Тест: `State.TERMINAL` содержит ровно `published/failed/rejected`; `next_state("queued") == "text_ready"`; `next_state("published")` бросает ошибку; `Post.from_row(row)` заполняет все поля
- [x] **Шаг 2.** Запустить, убедиться что падает
- [x] **Шаг 3.** Реализовать `models.py`: `State` (константы + `TERMINAL` + `TRANSITIONS` + `next_state()`), dataclasses `Project`, `Topic`, `Post`, `Asset`, `Run`, `Rejection` с методами `from_row`
- [x] **Шаг 4.** Тесты зелёные, коммит: `feat: модели данных и карта переходов`

---

### Задача 4: конфигурация проектов

**Файлы:** создать `factory/core/config.py`, `projects/demo/config.yaml`,
`projects/demo/prompts/voice.md`, `projects/demo/prompts/examples/example_1.md`,
`projects/demo/prompts/examples/example_2.md`, `projects/demo/templates/red_frame.json`;
тест `tests/test_config.py`

- [x] **Шаг 1.** Тест: `projects/demo/config.yaml` загружается, `cfg.limits.queue_buffer == 6`, `cfg.limits.posts_per_day == 2`, `cfg.review.mode == "auto"`
- [x] **Шаг 2.** Тест: конфиг без секции `vk` даёт `ConfigError`, в тексте которого есть имя файла, слово `vk` и подсказка что делать — а не питоновский трейсбек
- [x] **Шаг 3.** Тест: `llm.provider: nonexistent` даёт `ConfigError` со списком доступных провайдеров
- [x] **Шаг 4.** Тест: `resolve_secret("VK_TOKEN_DEMO")` при отсутствии переменной даёт ошибку с текстом из спеки («Не найден токен... Ожидается переменная... См. RUNBOOK.md»)
- [x] **Шаг 5.** Запустить, убедиться что падает
- [x] **Шаг 6.** Реализовать `config.py`: pydantic-модели `VkCfg`, `PersonaCfg`, `LlmCfg`, `ImageCfg`, `PublisherCfg`, `ContentCfg`, `ReviewCfg`, `LimitsCfg`, `ProjectConfig`; `load_project(slug)`; `resolve_secret(env_name, context)`; перевод `ValidationError` в `ConfigError` с русским текстом
- [x] **Шаг 7.** Создать файлы проекта `demo` (конфиг — как в разделе «Ключевые контракты», `voice.md` и примеры — короткие заглушки, `red_frame.json` — координаты плашки, цвет и толщина рамки, диапазон кегля)
- [x] **Шаг 8.** Тесты зелёные, коммит: `feat: конфигурация проектов и учебный проект demo`

---

### Задача 5: блокировка тика и хартбит

**Файлы:** создать `factory/core/lock.py`; тест `tests/test_lock.py`

- [x] **Шаг 1.** Тест: первый `acquire_tick_lock()` успешен, второй при живой блокировке — возвращает `None`
- [x] **Шаг 2.** Тест: блокировка с истёкшим `expires_at` перехватывается новым тиком
- [x] **Шаг 3.** Тест: выход из контекст-менеджера снимает блокировку даже при исключении внутри
- [x] **Шаг 4.** Тест: `write_heartbeat()` и `heartbeat_age_sec()` работают
- [x] **Шаг 5.** Запустить, убедиться что падает
- [x] **Шаг 6.** Реализовать `lock.py`: `tick_lock()` — контекст-менеджер поверх строки `meta['tick_lock']` (JSON `{holder, pid, token, expires_at}`), захват и перехват протухшей в одной транзакции; `refresh()` — продлить, **чтение, проверка владельца и запись строго в одной транзакции**; `force_unlock()` — для CLI; `write_heartbeat()` / `heartbeat_age_sec()` / `heartbeat_is_stale()`

  `token` — случайный на каждый запуск процесса. Пары «хост + pid» для опознания недостаточно: в Docker воркер всегда PID 1 на неизменном хосте, поэтому убитый процесс и его замена выглядели бы одинаково, и проверки владельца были бы безусловно истинными.
- [x] **Шаг 7.** Тесты зелёные, коммит: `feat: блокировка тика с TTL и хартбит`

---

### Задача 6: ретраи и учёт вызовов

**Файлы:** создать `factory/core/retry.py`; тест `tests/test_retry.py`

- [x] **Шаг 1.** Тест: успешный вызов пишет строку в `runs` с `ok=1` и ненулевым `duration_ms`
- [x] **Шаг 2.** Тест: функция, падающая с сетевой ошибкой дважды и успешная на третий раз, — отрабатывает успешно, в `runs` одна строка `ok=1`
- [x] **Шаг 3.** Тест: `httpx.HTTPStatusError` со статусом 429 и заголовком `Retry-After: 1` приводит к паузе перед повтором (пауза мокается, проверяется вызов)
- [x] **Шаг 4.** Тест: исчерпание попыток пишет `runs.ok=0` с текстом ошибки и пробрасывает исключение наверх
- [x] **Шаг 5.** Запустить, убедиться что падает
- [x] **Шаг 6.** Реализовать `retry.py`: декоратор `tracked_call(step_name)` — 3 попытки на сетевые ошибки и 429 (с уважением `Retry-After`), экспоненциальная пауза, замер времени, запись в `runs` (включая `cost_usd`, если функция вернула его в атрибуте результата)
- [x] **Шаг 7.** Тесты зелёные, коммит: `feat: единый декоратор ретраев и учёта вызовов`

---

### Задача 7: фабрика HTTP-клиентов

**Файлы:** создать `factory/core/http.py`; тест `tests/test_http.py`

- [x] **Шаг 1.** Тест: `client_for("llm")` при `LLM_PROXY=http://a` использует его; при отсутствии `LLM_PROXY`, но наличии `HTTPS_PROXY=http://b` — использует `b`; при отсутствии обоих — без прокси
- [x] **Шаг 2.** Тест: таймауты `connect=10`, `read=120`
- [x] **Шаг 3.** Запустить, убедиться что падает
- [x] **Шаг 4.** Реализовать `http.py`: `client_for(provider_name, *, base_url=None, headers=None) -> httpx.Client`
- [x] **Шаг 5.** Тесты зелёные, коммит: `feat: фабрика httpx-клиентов с прокси на провайдера`

---

### Задача 8: протоколы провайдеров и заглушки

**Файлы:** создать `factory/providers/base.py`, `factory/providers/registry.py`,
`factory/providers/llm/stub.py`, `factory/providers/images/stub.py`,
`factory/providers/publishers/stub.py`; тест `tests/test_providers_stub.py`

- [ ] **Шаг 1.** Тест: `build_providers(cfg)` по конфигу `demo` возвращает три stub-реализации
- [ ] **Шаг 2.** Тест: неизвестное имя провайдера даёт `ConfigError` со списком доступных
- [ ] **Шаг 3.** Тест: `StubLLM.complete(system, user, schema=PostDraft)` возвращает экземпляр `PostDraft` с непустыми `title` (≤ 60 символов, без точки в конце), `body`, `question`
- [ ] **Шаг 4.** Тест: `StubImages.generate(prompt, ...)` возвращает байты валидного PNG размером 1080×1350 (проверить через `PIL.Image.open`)
- [ ] **Шаг 5.** Тест: `StubPublisher.publish(post, assets)` создаёт файл в `FACTORY_TMP_DIR` и возвращает строку вида `stub_<post_id>`
- [ ] **Шаг 6.** Запустить, убедиться что падает
- [ ] **Шаг 7.** Реализовать `base.py` (три `Protocol` дословно из спеки + pydantic-схемы `PostDraft`, `FactcheckResult`, `ScenePrompts`), `registry.py`, три заглушки. Заглушки детерминированные: один и тот же вход даёт один и тот же выход
- [ ] **Шаг 8.** Тесты зелёные, коммит: `feat: протоколы провайдеров и stub-реализации`

---

### Задача 9: шаги пайплайна

**Файлы:** создать `factory/core/steps/__init__.py` и семь файлов шагов;
тест `tests/test_transitions.py`

- [ ] **Шаг 1.** Тест: `REGISTRY` покрывает все нетерминальные состояния и ни одного терминального
- [ ] **Шаг 2.** Тест на каждый переход отдельно (8 тестов): пост в состоянии X, вызвать обработчик, проверить `outcome == ADVANCED`, `next_state` правильный, и побочный эффект на месте:
  - `text` → заполнены `title`, `body`, `question`
  - `factcheck` → заполнен `factcheck_verdict`; при `content.factcheck: off` шаг проходит без вызова LLM
  - `prompts` → в `assets` появилось `1 + inline_count` строк с промптами и `local_path IS NULL`
  - `images` → у всех `assets` заполнен `local_path`, файлы существуют
  - `compose` → у обложки путь ведёт на существующий файл
  - `review` при `mode: auto` → `composed → in_review → approved` без участия человека
  - `review` при `mode: telegram` → из `in_review` возвращает `WAITING`
  - `publish` → заполнены `external_id` и `published_at`, тема переведена в `used`
- [ ] **Шаг 3.** Тест идемпотентности `images`: если у половины `assets` уже есть `local_path`, повторный вызов не трогает их (проверить по `mtime` файла и по числу вызовов провайдера)
- [ ] **Шаг 4.** Тест идемпотентности `publish`: пост с непустым `external_id` не вызывает `publisher.publish` повторно
- [ ] **Шаг 5.** Запустить, убедиться что падает
- [ ] **Шаг 6.** Реализовать `steps/__init__.py` (`Outcome`, `StepResult`, `StepContext`, `REGISTRY`) и семь шагов. Шаги вызывают провайдеров через `ctx.providers`, никакой сетевой специфики внутри
- [ ] **Шаг 7.** Тесты зелёные, коммит: `feat: шаги пайплайна на stub-провайдерах`

---

### Задача 10: пополнение очереди

**Файлы:** создать `factory/core/machine.py` (первая половина); тест `tests/test_queue.py`

- [ ] **Шаг 1.** Тест: 10 свободных тем, `queue_buffer=3` (задаётся в тесте, чтобы проверить, что значение читается из конфига, а не зашито) → после `replenish_queue` ровно 3 поста, 3 темы в статусе `taken`
- [ ] **Шаг 2.** Тест: повторный вызов `replenish_queue` не создаёт постов сверх буфера
- [ ] **Шаг 3.** Тест: посты в `published`, `failed` и `rejected` в буфере не считаются и освобождают место под новые
- [ ] **Шаг 4.** Тест: свободных тем нет → предупреждение в логе, исключения нет
- [ ] **Шаг 5.** Тест: `idem_key` создаваемого поста равен `demo:<topic_id>:0`
- [ ] **Шаг 6.** Запустить, убедиться что падает
- [ ] **Шаг 7.** Реализовать `replenish_queue(conn, project)` и `claim_free_topic(conn, project_id)` (атомарный `UPDATE topics SET status='taken' WHERE id = (SELECT id FROM topics WHERE project_id=? AND status='free' ORDER BY id LIMIT 1) RETURNING id`)
- [ ] **Шаг 8.** Тесты зелёные, коммит: `feat: пополнение очереди постов до буфера`

---

### Задача 11: продвижение постов, многошаговый тик, backoff

**Файлы:** дополнить `factory/core/machine.py`; тесты `tests/test_multistep.py`,
`tests/test_backoff.py`

- [ ] **Шаг 1.** Тест: `FACTORY_MAX_STEPS_PER_TICK=1` → один тик двигает пост ровно на одно состояние
- [ ] **Шаг 2.** Тест: `FACTORY_MAX_STEPS_PER_TICK=3` → один тик двигает пост из `queued` в `prompts_ready`
- [ ] **Шаг 3.** Тест: шаг вернул `WAITING` → цепочка обрывается, `retry_count` не вырос, состояние не изменилось
- [ ] **Шаг 4.** Тест: второй шаг цепочки бросил исключение → первый переход сохранён в базе, `retry_count == 1`, `next_attempt_at` в будущем
- [ ] **Шаг 5.** Тест backoff: паузы после отказов идут 10, 20, 40, 80 минут — ровно четыре значения. Пятый отказ переводит пост в `failed`, и пауза 160 минут не применяется никогда: `next_attempt_at` у поста в `failed` смысла не имеет. Проверить это явно, иначе формула и порог живут своей жизнью
- [ ] **Шаг 6.** Тест: посты с `next_attempt_at` в будущем тиком не берутся
- [ ] **Шаг 7.** Запустить, убедиться что падает
- [ ] **Шаг 8.** Реализовать `advance_post()`, `record_failure()`, `record_wait()`, `commit_transition()`, `due_posts()`, `tick()` (полный цикл: блокировка → по всем активным проектам пополнить очередь → продвинуть посты → хартбит)
- [ ] **Шаг 9.** Тесты зелёные, коммит: `feat: многошаговый тик стейт-машины с backoff`

---

### Задача 12: расписание и дневной лимит публикаций

**Файлы:** дополнить `factory/core/steps/publish.py`; тест `tests/test_publish_limit.py`

- [ ] **Шаг 1.** Тест: `posts_per_day=2`, два поста уже `published` сегодня → третий получает `WAITING`, а не ошибку, `retry_count` не растёт
- [ ] **Шаг 2.** Тест: счётчик считает по `published_at`, а не по `updated_at`. Пост опубликован вчера в 23:50 по Москве, сегодня его строку тронули (например, `post retry` обновил `updated_at`) — сегодняшний лимит он НЕ занимает
- [ ] **Шаг 2а.** Тест: счётчик считает в часовом поясе проекта, а не в UTC. Пост, опубликованный в 23:30 по Москве, относится к московским суткам, хотя в UTC это уже 20:30 тех же суток — а для `Asia/Vladivostok` тот же момент попадёт в следующие сутки
- [ ] **Шаг 3.** Тест: время вне слотов расписания → `WAITING("вне расписания публикаций")`
- [ ] **Шаг 4.** Тест: `FACTORY_IGNORE_SCHEDULE=1` → проверка расписания пропускается
- [ ] **Шаг 5.** Тест: при `FACTORY_IGNORE_SCHEDULE=1` каждый тик пишет в лог запись уровня WARNING со словами «расписание отключено»
- [ ] **Шаг 6.** Запустить, убедиться что падает
- [ ] **Шаг 7.** Реализовать `published_today(conn, project)` (по `published_at`) и `schedule_slot_open(project, now)` (окно: от времени слота до слота + 60 минут, слот считается использованным, если в нём уже была публикация); WARNING про отключённое расписание пишется в `machine.tick()`, а не в шаге, — иначе он не появится, когда публиковать нечего
- [ ] **Шаг 8.** Тесты зелёные, коммит: `feat: расписание публикаций и дневной лимит`

---

### Задача 13: отклонение поста и возврат темы

**Файлы:** создать `factory/core/reject.py`; тест `tests/test_rejection.py`

- [ ] **Шаг 1.** Тест: `reject_post(id, reason="trash")` → пост в `rejected`, тема снова `free`, `used_at` очищен, в `rejections` появилась строка со снапшотом (`title`, `body`, промпты)
- [ ] **Шаг 2.** Тест переиспользования: после отклонения `replenish_queue` берёт ту же тему и создаёт по ней новый пост
- [ ] **Шаг 3.** Тест: `idem_key` нового поста по той же теме равен `demo:<topic_id>:1` — уникальный индекс не срабатывает, `IntegrityError` не возникает
- [ ] **Шаг 4.** Тест: тему отклонили трижды → четвёртый пост получает `attempt = 3`, все четыре строки в `posts` живы
- [ ] **Шаг 5.** Запустить, убедиться что падает
- [ ] **Шаг 6.** Реализовать `reject.py`. Формат `idem_key` — `f"{slug}:{topic_id}:{attempt}"`, где `attempt` = число прошлых отклонений темы (`SPEC.md` уже обновлён). Защита от дубля публикации держится не на этом ключе, а на проверке `external_id IS NULL` перед вызовом публикации
- [ ] **Шаг 7.** Тесты зелёные, коммит: `feat: отклонение поста с возвратом темы в очередь`

---

### Задача 14: воркер и CLI

**Файлы:** создать `factory/workers/tick.py`, `factory/cli.py`; тест `tests/test_cli.py`

- [ ] **Шаг 1.** Тест через `typer.testing.CliRunner`: `factory init` создаёт файл базы и применяет миграции; повторный вызов не ломается
- [ ] **Шаг 2.** Тест: `factory project add demo` регистрирует проект; повторный вызов даёт понятное сообщение, а не трейсбек
- [ ] **Шаг 3.** Тест: `factory topics import demo <файл>` загружает темы построчно, пустые строки и дубли пропускает, печатает сколько загружено
- [ ] **Шаг 4.** Тест: `factory run --once` отрабатывает один тик и завершается с кодом 0
- [ ] **Шаг 5.** Тест: `factory post show <id>` печатает состояние, заголовок, число картинок, последнюю ошибку
- [ ] **Шаг 6.** Тест: `factory doctor` на здоровой системе даёт код 0; при отсутствии базы — код 1 и текст «что делать»
- [ ] **Шаг 6а.** Тест: `factory doctor` при недоступном на запись каталоге данных печатает подсказку про `FACTORY_DATA_DIR` и завершается с кодом 1 — без `PermissionError` и трейсбека. `doctor` вызывает `ensure_data_dir()` первым, до попытки открыть базу
- [ ] **Шаг 7.** Запустить, убедиться что падает
- [ ] **Шаг 8.** Реализовать `workers/tick.py` (`run_once()`, `run_loop()` со `sleep(tick_interval_sec())` и корректной обработкой `SIGTERM`) и `cli.py` на typer: `init`, `doctor`, `unlock`, `run`, группы `project` (`add`, `list`), `topics` (`import`, `list`), `post` (`create`, `show`, `list`, `retry`, `reject`). У каждой команды — русская строка справки; все ошибки ловятся и печатаются как `FactoryError`, без трейсбеков
- [ ] **Шаг 9.** Тесты зелёные, коммит: `feat: воркер тика и CLI на typer`

---

### Задача 15: краш-тесты и параллельность

**Файлы:** тесты `tests/test_crash_resume.py`, `tests/test_concurrent.py`

- [ ] **Шаг 1.** Тест возобновляемости: запустить `factory run --loop` подпроцессом при `FACTORY_MAX_STEPS_PER_TICK=3` и заглушке, которая на третьем шаге зависает; убить `SIGKILL`; проверить, что первые два перехода в базе сохранены; запустить `factory run --once` и убедиться, что пост поехал дальше с третьего шага, а не с начала
- [ ] **Шаг 2.** Тест «оплаченное не переделывается»: после краша на шаге `images` уже сгенерированные файлы не перегенерируются — счётчик вызовов stub-провайдера это подтверждает
- [ ] **Шаг 3.** Тест блокировки тика: запустить два `factory run --once` одновременно (`multiprocessing`); проверить, что постов создано ровно `queue_buffer`, каждая тема занята не более одного раза, и ни один пост не опубликован дважды
- [ ] **Шаг 4.** Тест гонки за темами — **отдельно от шага 3**. Шаг 3 проверяет только блокировку: второй процесс не получает лок и выходит, не дойдя до `claim_free_topic`, поэтому атомарность самого захвата остаётся непроверенной. Здесь 8 потоков дёргают `claim_free_topic()` напрямую, минуя блокировку, на 5 свободных темах. Ожидание: ровно 5 успешных захватов и 3 отказа (`None`), ни одна тема не досталась двоим, дублей `topic_id` в результатах нет
- [ ] **Шаг 5.** Тест перехвата протухшей блокировки: убитый процесс оставил `tick_lock`; при `FACTORY_LOCK_TTL_SEC=1` следующий тик её забирает
- [ ] **Шаг 6.** Все тесты зелёные, коммит: `test: возобновляемость после краша и защита от параллельных тиков`

---

### Задача 16: документация

**Файлы:** создать `README.md`, `RUNBOOK.md`, `TROUBLESHOOTING.md`, `CLAUDE.md`

- [ ] **Шаг 1.** `README.md`: что это, установка с нуля на macOS (реальные команды, без плейсхолдеров), первый запуск до первого «опубликованного» поста-заглушки, что появится на следующих этапах. Для локальной разработки указать конкретные значения, готовые к копипасту: `export FACTORY_DATA_DIR=~/factory-data`, `export FACTORY_PROJECTS_DIR=$(pwd)/projects` — умолчания `/data` и `/app/projects` рассчитаны на контейнер и на macOS без `sudo` не работают
- [ ] **Шаг 2.** `RUNBOOK.md` — рецепты, доступные на этом этапе: запустить/остановить, посмотреть что происходит сейчас, посмотреть логи, поставить на паузу, добавить темы, посмотреть сколько тем осталось, отклонить пост, перезапустить застрявший пост, снять зависшую блокировку, сделать бэкап базы
- [ ] **Шаг 3.** `TROUBLESHOOTING.md`: таблица симптом → причина → команда проверки → лечение. Минимум пять строк из реальных ситуаций этапа (посты не двигаются; «темы закончились»; блокировка висит; база заблокирована; конфиг не проходит валидацию)
- [ ] **Шаг 4.** `CLAUDE.md`: архитектура в двух абзацах, карта каталогов, конвенции, как добавить шаг пайплайна, как добавить провайдера, как гонять тесты
- [ ] **Шаг 5.** `CLAUDE.md`, раздел «Задолженность Этапа 5» — записать обе задачи дословно:
  1. **Алерт «нечем публиковать»**: срабатывает, когда свободных тем не осталось ИЛИ ближайший слот расписания нечем закрыть. Не вешать алерт на «N постов ждут ревью» — это нормальная рабочая ситуация, алерт на неё станет шумом
  2. **Мягкий таймаут на `WAITING`**: пост, висящий в одном состоянии дольше суток, даёт уведомление владельцу. В `failed` не переводить — ожидание человека это не ошибка
- [ ] **Шаг 6.** Коммит: `docs: README, RUNBOOK, TROUBLESHOOTING, CLAUDE.md для Этапа 1`

---

### Задача 17: приёмка этапа

- [ ] **Шаг 1.** `uv run pytest -v` — все тесты зелёные, ни одного пропущенного
- [ ] **Шаг 2.** Проверить изоляцию от сети: `uv run pytest tests/test_no_network.py -v` — предохранитель из `conftest.py` жив и роняет любой реальный запрос с понятным сообщением
- [ ] **Шаг 3.** Сквозной прогон по рецепту из `README.md` при `FACTORY_TICK_INTERVAL_SEC=5`, `FACTORY_IGNORE_SCHEDULE=1`: база → проект → 10 тем → `run --loop` → через минуту два поста в `published`
- [ ] **Шаг 4.** Проверка глазами: `factory post show 1` показывает заголовок, текст, 4 файла картинок на диске, фейковый `external_id`
- [ ] **Шаг 5.** Проверка возобновляемости руками: `run --loop`, `kill -9` на середине, повторный запуск — пост доезжает
- [ ] **Шаг 6.** Проверка приёмки по спеке: человек, не открывая исходники, выполняет рецепты из `RUNBOOK.md` и получает ожидаемый результат
- [ ] **Шаг 7.** Финальный коммит и тег: `git tag stage-1`

---

## Что НЕ входит в Этап 1

Чтобы не расползлось: реальные вызовы LLM, картинок и ВК; сборка обложки на Pillow
(`compose/cover.py`); Telegram-бот и кнопки ревью; воркер комментариев; Dockerfile,
compose и CI; бэкапы по расписанию; ежедневная сводка; контроль стоимости поста.
Всё это — этапы 2–7, и трогать их в этом этапе нельзя.
