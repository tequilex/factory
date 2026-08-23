-- Начальная схема. Соответствует разделу «Схема БД» в SPEC.md.
--
-- CHECK-ограничения на состояния стоят намеренно: опечатка в имени состояния
-- иначе означала бы пост, который молча стоит навсегда. Добавление нового
-- состояния потребует отдельной миграции с пересборкой таблицы — это осознанная
-- плата за то, чтобы такой баг падал сразу и громко.

CREATE TABLE projects (
    id            INTEGER PRIMARY KEY,
    slug          TEXT UNIQUE NOT NULL,
    config_path   TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE topics (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    title         TEXT NOT NULL,
    angle         TEXT,
    status        TEXT NOT NULL DEFAULT 'free'
                  CHECK (status IN ('free', 'taken', 'used')),
    used_at       TEXT
);

CREATE INDEX idx_topics_free ON topics(project_id, status) WHERE status = 'free';

CREATE TABLE posts (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    topic_id      INTEGER NOT NULL REFERENCES topics(id),
    -- Формат: {slug}:{topic_id}:{attempt}. Третий сегмент нужен потому, что
    -- отклонённая тема возвращается в free и по ней создаётся новый пост.
    idem_key      TEXT UNIQUE NOT NULL,
    state         TEXT NOT NULL DEFAULT 'queued'
                  CHECK (state IN (
                      'queued', 'text_ready', 'factchecked', 'prompts_ready',
                      'images_ready', 'composed', 'in_review', 'approved',
                      'published', 'failed', 'rejected'
                  )),
    title         TEXT,
    body          TEXT,
    question      TEXT,
    factcheck_verdict TEXT
                  CHECK (factcheck_verdict IS NULL
                         OR factcheck_verdict IN ('ok', 'fixed', 'uncertain')),
    factcheck_notes TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    next_attempt_at TEXT,
    scheduled_at  TEXT,
    external_id   TEXT,
    -- Пишется ровно один раз, в момент публикации. Дневной лимит считается по
    -- нему, а не по updated_at: updated_at меняется при любой правке строки.
    published_at  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX idx_posts_active ON posts(state, next_attempt_at);
CREATE INDEX idx_posts_published ON posts(project_id, published_at);

CREATE TABLE assets (
    id            INTEGER PRIMARY KEY,
    post_id       INTEGER NOT NULL REFERENCES posts(id),
    kind          TEXT NOT NULL CHECK (kind IN ('cover', 'inline')),
    position      INTEGER NOT NULL,
    prompt        TEXT,
    seed          INTEGER,
    local_path    TEXT,
    external_ref  TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (post_id, kind, position)
);

CREATE INDEX idx_assets_post ON assets(post_id, position);

CREATE TABLE comments (
    id            INTEGER PRIMARY KEY,
    post_id       INTEGER NOT NULL REFERENCES posts(id),
    external_id   TEXT UNIQUE NOT NULL,
    author        TEXT,
    text          TEXT,
    replied_at    TEXT
);

CREATE TABLE runs (
    id            INTEGER PRIMARY KEY,
    post_id       INTEGER REFERENCES posts(id),
    step          TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    duration_ms   INTEGER,
    cost_usd      REAL,
    error         TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX idx_runs_post ON runs(post_id, created_at);

CREATE TABLE rejections (
    id            INTEGER PRIMARY KEY,
    post_id       INTEGER NOT NULL REFERENCES posts(id),
    reason        TEXT NOT NULL CHECK (reason IN ('text', 'images', 'trash')),
    snapshot      TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX idx_rejections_post ON rejections(post_id);

-- Служебное хранилище: блокировка тика и хартбит. В SPEC.md таблицы для них
-- нет, но без них не собирается ни защита от наложения запусков, ни healthcheck.
CREATE TABLE meta (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
