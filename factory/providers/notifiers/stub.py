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
    alerts: list[str] = field(default_factory=list)
    _next_message_id: int = 1000

    def send_for_review(
        self,
        *,
        chat_id: int,
        project: str,
        title: str,
        body: str,
        warning: str | None,
        images: list[str],
        post_id: int,
    ) -> ReviewMessage:
        self._next_message_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "project": project,
                "title": title,
                "body": body,
                "warning": warning,
                "images": list(images),
                "post_id": post_id,
            }
        )
        log.info("ревью-сообщение не отправлено: заглушка", extra={"post_id": post_id})
        return ReviewMessage(chat_id=chat_id, message_id=self._next_message_id)

    def alert(self, *, chat_id: int, text: str) -> None:
        self.alerts.append(text)
        log.info("тревога не отправлена: заглушка", extra={"text": text[:120]})
