"""Fake image generator: a coloured rectangle with the prompt written on it.

Real PNG bytes at the real size, so everything downstream — writing to disk,
cover composition, VK upload — works against something that behaves like the
genuine article. The colour is derived from the prompt, which makes it obvious at
a glance whether two assets came from the same scene.
"""

from __future__ import annotations

import hashlib
import io
import textwrap

from PIL import Image, ImageDraw

from factory.providers.base import IMAGE_HEIGHT, IMAGE_WIDTH


def _colour(prompt: str, seed: int | None) -> tuple[int, int, int]:
    digest = hashlib.sha256(f"{prompt}:{seed}".encode()).digest()
    # Keep it dark enough for white text to stay readable.
    return (digest[0] % 140 + 40, digest[1] % 140 + 40, digest[2] % 140 + 40)


class StubImages:
    """Заглушка генератора картинок: рисует прямоугольник с текстом промпта."""

    name = "stub"

    def __init__(self, *, model: str = "stub") -> None:
        self.model = model
        self.calls = 0

    def generate(
        self,
        prompt: str,
        *,
        lora: str | None = None,
        seed: int | None = None,
        width: int = IMAGE_WIDTH,
        height: int = IMAGE_HEIGHT,
    ) -> bytes:
        self.calls += 1

        image = Image.new("RGB", (width, height), _colour(prompt, seed))
        draw = ImageDraw.Draw(image)

        lines = textwrap.wrap(prompt, width=34) or ["(пустой промпт)"]
        lines.append("")
        lines.append(f"seed={seed} lora={lora or '—'}")
        lines.append(f"{width}x{height}")

        y = height // 2 - len(lines) * 12
        for line in lines:
            draw.text((48, y), line, fill=(255, 255, 255))
            y += 24

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
