"""Поддерживаемая в редакторе разметка Telegram."""

import re
from html import escape

TOKENS = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)|\*\*([^*\n]+)\*\*|_([^_\n]+)_")


def format_text(text: str) -> tuple[str, int, str | None]:
    parts = []
    plain = []
    start = 0
    for match in TOKENS.finditer(text):
        before = text[start : match.start()]
        parts.append(escape(before))
        plain.append(before)
        label, url, bold, italic = match.groups()
        value = label or bold or italic
        plain.append(value)
        if url:
            parts.append(f'<a href="{escape(url, quote=True)}">{escape(value)}</a>')
        else:
            tag = "b" if bold else "i"
            parts.append(f"<{tag}>{escape(value)}</{tag}>")
        start = match.end()
    if not parts:
        return text, len(text), None
    parts.append(escape(text[start:]))
    plain.append(text[start:])
    return "".join(parts), sum(map(len, plain)), "HTML"
