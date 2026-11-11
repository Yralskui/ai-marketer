"""Загрузка промптов из .md-файлов этой папки.

Подстановки в промптах — `{{name}}` (двойные скобки, чтобы не конфликтовать с JSON).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@lru_cache
def load_prompt(name: str) -> str:
    """Текст промпта `<name>.md`."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Промпт не найден: {path}")
    return path.read_text(encoding="utf-8").strip()


def render_prompt(name: str, **variables: object) -> str:
    """Промпт с подставленными переменными. Неизвестная переменная — ошибка."""
    template = load_prompt(name)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise KeyError(f"В промпте {name!r} не передана переменная {key!r}")
        return str(variables[key])

    return _VAR.sub(replace, template)
