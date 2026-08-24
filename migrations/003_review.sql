-- Этап 5: Telegram-ревью.
--
-- Две части. Колонки к posts добавляются обычным ALTER TABLE. А вот новая
-- причина отказа требует пересборки rejections: изменить CHECK в SQLite иначе
-- нельзя.

ALTER TABLE posts ADD COLUMN review_chat_id INTEGER;
ALTER TABLE posts ADD COLUMN review_message_id INTEGER;
ALTER TABLE posts ADD COLUMN decided_at TEXT;
ALTER TABLE posts ADD COLUMN decided_by INTEGER;

-- Причина 'scenes' — «сцены придуманы плохо», в отличие от 'images' —
-- «сцены хорошие, нарисовано плохо». Для будущего датасета это разные сигналы:
-- первый учит придумывать, второй — рисовать.
CREATE TABLE rejections_new (
    id            INTEGER PRIMARY KEY,
    post_id       INTEGER NOT NULL REFERENCES posts(id),
    reason        TEXT NOT NULL CHECK (reason IN ('text', 'scenes', 'images', 'trash')),
    snapshot      TEXT,
    created_at    TEXT NOT NULL
);

-- Колонки перечислены явно: SELECT * сломается при следующем изменении схемы.
INSERT INTO rejections_new (id, post_id, reason, snapshot, created_at)
SELECT id, post_id, reason, snapshot, created_at FROM rejections;

DROP TABLE rejections;
ALTER TABLE rejections_new RENAME TO rejections;

-- Индекс ушёл вместе со старой таблицей.
CREATE INDEX idx_rejections_post ON rejections(post_id);
