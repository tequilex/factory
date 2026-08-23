#!/usr/bin/env python
"""Проверка тестов мутациями.

Обязательный шаг чеклиста самокритики (CLAUDE.md): для каждого нового теста
придумать изменение в коде, которое должно его уронить, и убедиться, что оно
действительно роняет. Тест, переживающий поломку кода, — хуже отсутствующего.

Мутации описываются в файле-задании: JSON со списком правок. Каждая правка —
подстрока в исходнике и то, на что её заменить.

    uv run python scripts/mutation_check.py scripts/mutations/lock.json

Формат файла:

    {
      "module": "factory/core/lock.py",
      "tests": "tests/test_lock.py",
      "mutations": {
        "название мутации": ["что заменить", "на что заменить"]
      }
    }

Скрипт всегда восстанавливает исходник — в том числе если прогон упал или его
прервали. Код возврата 1, если хотя бы одна мутация не поймана.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_tests(tests: str | list[str]) -> tuple[bool, list[str]]:
    # "tests" может быть строкой с несколькими путями через пробел или списком.
    targets = tests.split() if isinstance(tests, str) else list(tests)
    result = subprocess.run(
        ["uv", "run", "pytest", *targets, "-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    failed = [
        line.split(" - ")[0].removeprefix("FAILED ").strip()
        for line in result.stdout.splitlines()
        if line.startswith("FAILED")
    ]
    return result.returncode == 0, failed


def _restore_on_signal(module: Path, original: str):
    """Возвращает исходник, если скрипт прервали или прибили по таймауту.

    Без этого прерванный прогон оставляет в рабочем дереве изменённый модуль —
    и следующий человек (или агент) видит непонятно откуда взявшуюся правку.
    """

    def handler(signum, frame):
        module.write_text(original, encoding="utf-8")
        raise SystemExit(130)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handler)


def main(task_path: str) -> int:
    task = json.loads(Path(task_path).read_text(encoding="utf-8"))
    module = REPO_ROOT / task["module"]
    tests = task["tests"]
    original = module.read_text(encoding="utf-8")
    _restore_on_signal(module, original)

    passed_clean, _ = run_tests(tests)
    if not passed_clean:
        print("Тесты не проходят на неизменённом коде — сначала почини их.")
        return 1

    escaped = 0
    try:
        for name, (old, new) in task["mutations"].items():
            if old not in original:
                print(f"ЯКОРЬ НЕ НАЙДЕН     {name}")
                escaped += 1
                continue

            module.write_text(original.replace(old, new, 1), encoding="utf-8")
            survived, failed = run_tests(tests)
            module.write_text(original, encoding="utf-8")

            if survived:
                print(f"!!! ПРОПУЩЕНО !!!   {name}")
                escaped += 1
            else:
                print(f"ПОЙМАНО             {name}")
                for test in failed[:2]:
                    print(f"                    ← {test}")
    finally:
        module.write_text(original, encoding="utf-8")

    print()
    if escaped:
        print(f"Мутаций не поймано: {escaped}. Тесты нужно усилить.")
        return 1
    print(f"Все {len(task['mutations'])} мутаций пойманы.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
