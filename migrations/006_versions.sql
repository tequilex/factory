-- Варианты поста.
--
-- До этого откат уничтожал предыдущий вариант: картинки писались в файлы с
-- одинаковыми именами и перезаписывались, текст занулялся. Посмотреть второй
-- вариант значило потерять первый — то есть выбирать было нельзя в принципе,
-- можно было только соглашаться или переделывать вслепую.
--
-- Теперь каждый доведённый до ревью вариант остаётся: со своим текстом, своими
-- промптами и своими файлами. Владелец листает сообщения и публикует любой.

CREATE TABLE post_versions (
    id            INTEGER PRIMARY KEY,
    post_id       INTEGER NOT NULL REFERENCES posts(id),
    number        INTEGER NOT NULL,
    title         TEXT,
    body          TEXT,
    question      TEXT,
    factcheck_verdict TEXT,
    factcheck_notes   TEXT,
    -- Промпты, seed'ы и пути к файлам этого варианта, json.
    assets        TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (post_id, number)
);

CREATE INDEX idx_versions_post ON post_versions(post_id, number);

-- Номер варианта, который делается прямо сейчас. Растёт при каждом откате;
-- по нему же раскладываются файлы картинок, чтобы варианты не затирали друг
-- друга на диске.
ALTER TABLE posts ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
