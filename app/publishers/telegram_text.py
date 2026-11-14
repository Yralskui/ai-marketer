"""Разметка редактора и совместимость с Markdown старых постов."""

import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

OPEN = "<telegram-format>"
CLOSE = "</telegram-format>"
TAGS = {"b", "i", "u", "s", "tg-spoiler", "a", "code", "pre", "blockquote"}
EXCLUSIVE = {"code", "pre"}
LEGACY = re.compile(
    r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)|\*\*([^*\n]+)\*\*|__([^_\n]+)__|"
    r"(?<![\w_])_([^_\n]+)_(?![\w_])|~~([^~\n]+)~~|\|\|([^|\n]+)\|\||"
    r"```(?:[\w+-]+\n)?([\s\S]+?)```|`([^`\n]+)`"
)


class FormattedPost(HTMLParser):
    def __init__(self, markup: str) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.plain: list[str] = []
        self.html: list[str] = []
        self.feed(markup)
        self.close()
        if self.stack:
            raise ValueError("Некорректное форматирование поста.")

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if (
            tag not in TAGS
            or tag in self.stack
            or EXCLUSIVE.intersection(self.stack)
            or (tag in EXCLUSIVE and self.stack)
            or (tag == "blockquote" and self.stack)
        ):
            raise ValueError("Недопустимое сочетание форматирования.")
        value = ""
        if tag == "a":
            url = attributes.get("href") or ""
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Укажите ссылку HTTP или HTTPS.")
            value = f' href="{escape(url, quote=True)}"'
        elif tag == "blockquote" and "expandable" in attributes:
            value = " expandable"
        self.stack.append(tag)
        self.html.append(f"<{tag}{value}>")

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError("Некорректное форматирование поста.")
        self.html.append(f"</{tag}>")

    def handle_data(self, data):
        self.plain.append(data)
        self.html.append(escape(data))


def formatted_post(text: str) -> FormattedPost | None:
    if text.startswith(OPEN) and text.endswith(CLOSE):
        return FormattedPost(text[len(OPEN) : -len(CLOSE)])
    return None


def plain_text(text: str) -> str:
    formatted = formatted_post(text)
    return "".join(formatted.plain) if formatted is not None else text


def format_text(text: str) -> tuple[str, int, str | None]:
    formatted = formatted_post(text)
    if formatted is not None:
        return "".join(formatted.html), sum(map(len, formatted.plain)), "HTML"
    parts = []
    length = 0
    start = 0
    for match in LEGACY.finditer(text):
        before = text[start : match.start()]
        parts.append(escape(before))
        length += len(before)
        label, url, *runs = match.groups()
        if url:
            parts.append(f'<a href="{escape(url, quote=True)}">{escape(label)}</a>')
            length += len(label)
        else:
            tag, value = next(
                (tag, value)
                for tag, value in zip(
                    ("b", "u", "i", "s", "tg-spoiler", "pre", "code"), runs, strict=True
                )
                if value is not None
            )
            parts.append(f"<{tag}>{escape(value)}</{tag}>")
            length += len(value)
        start = match.end()
    if not parts:
        return text, len(text), None
    parts.append(escape(text[start:]))
    return "".join(parts), length + len(text[start:]), "HTML"
