"""Заглушка уведомлений: всё складывает в память, наружу не ходит.

Нужна не только тестам. С ``notifier: stub`` систему можно гонять целиком, не
заводя бота — посты будут доходить до ревью и ждать там, а на что именно они
ждут, видно в логе и в ``factory post show``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from factory.core.logging import get_logger
from factory.providers.base import ReviewMessage

log = get_logger(__name__)


@dataclass
class StubNotifier:
    """Ничего не отправляет, но помнит, что просили отправить."""

    name: str = "stub"
    sent: list[dict] = field(default_factory=list)
    albums: list[dict] = field(default_factory=list)
    finished: list[dict] = field(default_factory=list)
    alert_keyboards: list[dict | None] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    _next_message_id: int = 1000

    def send_album(self, *, chat_id: int, caption: str, images: list[str]) -> int | None:
        if not images:
            return None
        self._next_message_id += 1
        self.albums.append({"chat_id": chat_id, "caption": caption, "images": list(images)})
        return self._next_message_id

    def send_review_text(
        self,
        *,
        chat_id: int,
        project: str,
        title: str,
        body: str,
        warning: str | None,
        post_id: int,
        reply_to: int | None = None,
        version: int = 1,
        total: int = 1,
    ) -> ReviewMessage:
        self._next_message_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "project": project,
                "title": title,
                "body": body,
                "warning": warning,
                "post_id": post_id,
                "reply_to": reply_to,
                "version": version,
                "total": total,
            }
        )
        log.info("ревью-сообщение не отправлено: заглушка", extra={"post_id": post_id})
        return ReviewMessage(chat_id=chat_id, message_id=self._next_message_id)

    def finish_review(self, *, chat_id: int, message_id: int, text: str) -> None:
        self.finished.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    def alert(self, *, chat_id: int, text: str, keyboard: dict | None = None) -> None:
        self.alerts.append(text)
        self.alert_keyboards.append(keyboard)
        log.info("тревога не отправлена: заглушка", extra={"text": text[:120]})
