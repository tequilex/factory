#!/usr/bin/env python
"""Одна картинка по одной сцене — чтобы подбирать внешность, не гоняя пост.

Полный пост стоит около 6.7 ₽ и минут пять: текст, фактчек, промпты, четыре
картинки, обложка. Подбирать по нему внешность девушки — дорого и медленно, а
подбирать приходится долго: карточка примет пишется на глаз.

Этот скрипт делает ровно один платный вызов той же моделью, тем же провайдером
и с тем же эталонным портретом, что и боевой шаг. Отличие только одно: сцену вы
задаёте сами, а не сочиняет её модель по тексту поста.

    uv run python scripts/preview_character.py \
        --project vk_local \
        --scene "checking the engine oil, hood open, daylight" \
        --out ~/Desktop/proba.png

Приметы и стиль берутся из конфига проекта. Чтобы попробовать другие, не правя
конфиг:

    ... --character "woman, 30 years old, red curly hair, freckles"
    ... --style "documentary photo, harsh midday sun"

С заголовком скрипт заодно соберёт обложку — видно, не закрывает ли плашка
лицо:

    ... --title "Проверил масло на холодную — и зря"

Сеть настоящая, деньги настоящие. Цена каждого вызова печатается.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def load_env(path: Path) -> int:
    """Забрать переменные из файла секретов, не перетирая уже заданные.

    Скрипт запускают руками из терминала, где ключей обычно нет. Просить
    владельца экспортировать пять переменных перед каждым запуском — верный
    способ получить «почему не работает» на пустом ключе.
    """
    if not path.is_file():
        return 0

    taken = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())
        taken += 1
    return taken


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Одна картинка по одной сцене: подбор внешности персонажа."
    )
    parser.add_argument("--project", required=True, help="слаг проекта, например vk_local")
    parser.add_argument("--scene", required=True, help="сцена по-английски, без примет")
    parser.add_argument("--out", default="proba.png", help="куда положить картинку")
    parser.add_argument("--character", help="приметы вместо тех, что в конфиге")
    parser.add_argument("--style", help="стиль съёмки вместо того, что в конфиге")
    parser.add_argument("--seed", type=int, help="повторить тот же кадр")
    parser.add_argument("--title", help="собрать обложку с этим заголовком")
    parser.add_argument(
        "--no-reference",
        action="store_true",
        help="без эталонного портрета — видно, что держится одними словами",
    )
    parser.add_argument(
        "--env",
        default=str(Path.home() / "factory-data" / ".env"),
        help="файл с ключами",
    )
    args = parser.parse_args()

    env_path = Path(args.env).expanduser()
    load_env(env_path)
    os.environ.setdefault("FACTORY_PROJECTS_DIR", str(REPO_ROOT / "projects"))
    # Чтобы сообщения об ошибках называли тот файл ключей, из которого скрипт
    # действительно читал. Иначе провайдер советует править /data/.env — путь
    # внутри контейнера, которого на ноутбуке нет.
    os.environ.setdefault("FACTORY_DATA_DIR", str(env_path.parent))

    from factory.compose import cover
    from factory.core.config import load_project
    from factory.core.errors import FactoryError
    from factory.core.retry import cost_of
    from factory.core.steps.prompts import scene_prompt
    from factory.providers.base import IMAGE_HEIGHT, IMAGE_WIDTH
    from factory.providers.registry import build_providers

    try:
        config = load_project(args.project)
        providers = build_providers(config)
    except FactoryError as exc:
        print(exc, file=sys.stderr)
        return 1

    images = providers.images
    if args.no_reference:
        # Тот же провайдер, но без образца: разница видна сразу и объясняет,
        # за что именно отвечает эталонный портрет.
        images.reference_url = None

    prompt = scene_prompt(
        args.character if args.character is not None else config.image.character,
        args.scene,
        args.style if args.style is not None else config.image.scene_style,
    )

    print(f"проект:   {config.slug}")
    print(f"модель:   {config.image.model}")
    print(f"образец:  {'да' if getattr(images, 'reference_url', None) else 'нет'}")
    print(f"промпт:   {prompt}")
    print("рисую…")

    try:
        data = images.generate(
            prompt, seed=args.seed, width=IMAGE_WIDTH, height=IMAGE_HEIGHT
        )
    except FactoryError as exc:
        print(exc, file=sys.stderr)
        return 1

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)

    price = cost_of(data)
    print(f"готово:   {out}  ({len(data) // 1024} КБ)")
    print(f"цена:     {price if price is not None else 'провайдер не сказал'}")

    if args.title:
        # Обложка собирается тем же кодом, что в бою: заголовок на картинке
        # рисуется только локально, у нейросети про русский текст просить нечего.
        composed = out.with_name(out.stem + "-обложка.png")
        composed.write_bytes(cover.render(data, args.title, config.cover_template_path))
        print(f"обложка:  {composed}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
