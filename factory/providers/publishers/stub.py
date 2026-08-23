"""Fake publisher: writes the post to a file and returns a plausible id.

Writing a real file matters — it is what lets a person look at what *would* have
been published without a VK group, a token, or the risk of posting placeholder
text to a live audience.
"""

from __future__ import annotations

import json

from factory.core import paths
from factory.core.clock import now_utc, to_iso
from factory.core.logging import get_logger

log = get_logger(__name__)


class StubPublisher:
    """Заглушка публикатора: складывает пост в файл, наружу ничего не отправляет."""

    name = "stub"

    def __init__(self, *, group_id: int = 0) -> None:
        self.group_id = group_id
        self.calls = 0

    def publish(self, post, assets) -> str:
        self.calls += 1

        target = paths.tmp_dir() / "published"
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"post_{post.id}.json"

        payload = {
            "post_id": post.id,
            "published_at": to_iso(now_utc()),
            "title": post.title,
            "body": post.body,
            "question": post.question,
            "attachments": [
                {"kind": asset.kind, "position": asset.position, "path": asset.local_path}
                for asset in assets
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        external_id = f"stub_{post.id}"
        log.info(
            "пост «опубликован» заглушкой",
            extra={"post_id": post.id, "external_id": external_id, "file": str(path)},
        )
        return external_id

    def fetch_comments(self, external_id: str) -> list:
        return []

    def reply(self, external_comment_id: str, text: str) -> None:
        log.info("ответ на комментарий заглушен", extra={"comment_id": external_comment_id})
