"""Поиск по небольшому Markdown-каталогу продукта и интеграций."""

import re
from functools import lru_cache
from pathlib import Path

DIRECTORY = Path(__file__).with_name("knowledge")
WORD = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)


def terms(text: str) -> set[str]:
    return {
        word[:5]
        for word in WORD.findall(text.casefold().replace("ё", "е"))
        if len(word) >= 3 or word == "vk"
    }


@lru_cache
def sections(corpus: str) -> tuple[tuple[str, str], ...]:
    markdown = (DIRECTORY / f"{corpus}.md").read_text(encoding="utf-8")
    return tuple(tuple(part.strip().split("\n", 1)) for part in markdown.split("## ")[1:])


def search_help(query: str, corpus: str) -> str:
    keywords = terms(query)
    ranked = sorted(
        sections(corpus),
        key=lambda section: (
            3 * len(keywords & terms(section[0])) + len(keywords & terms(section[1]))
        ),
        reverse=True,
    )
    if corpus == "platform" and re.search(r"проект|умеешь|умеет|возможност|функци", query, re.I):
        overview = "Возможности: " + ", ".join(title for title, _ in sections(corpus)[1:])
        intro = sections(corpus)[0][1]
        return f"{intro}\n{overview}\n\n" + "\n\n".join(
            f"## {title}\n{body}" for title, body in ranked[:3]
        )
    return "\n\n".join(f"## {title}\n{body}" for title, body in ranked[:3])
