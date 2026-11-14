"""Детерминированная заглушка LLM: работает без ключей и интернета.

Зачем: разрабатывать и демонстрировать сквозной сценарий бесплатно, а тесты делать
стабильными. Поведение выбирается по `task`:

- "agent"           — по ключевым словам выбирает инструмент; после результата
                      инструмента пишет короткий итоговый ответ;
- "write_post"      — пост из библиотеки шаблонов по строке «Тема: ...» (для
                      САЛАВАТСТЕКЛО — по брендбуку, см. post_templates.py);
- "revise_post"     — простые правки («короче», «эмодзи», «подробнее», ...);
- "extract_profile" — JSON-профиль бизнеса из сырого текста эвристиками.

⚠️ Mock опирается на формат user-сообщений, которые собирают сервисы
(строки «Тема:», «Название бизнеса:», блок <<< >>> и «Правка клиента:»).
Меняете формат в services/posts.py — поправьте и разбор здесь.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from app.llm.base import LLMProvider, LLMResponse, Message, ToolCall
from app.llm.post_templates import next_topics, render_post

IMAGE_WORDS = ("картин", "фото", "изображ", "иллюстрац", "визуал")
REVISE_WORDS = ("перепиш", "измени", "исправ", "короч", "сократ", "добав", "убер", "поправ")
POST_WORDS = ("пост", "напиши", "подготов", "публикац", "анонс", "текст для")


class MockLLMProvider(LLMProvider):
    name = "mock"

    def __init__(self) -> None:
        self._call_counter = 0

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        json_mode: bool = False,
        task: str | None = None,
    ) -> LLMResponse:
        last_user = _last_content(messages, "user")
        current = last_user.rsplit("\n\nТекущий запрос:\n", 1)[-1]
        if task == "classify_intent":
            call = _route(current)
            low = current.casefold()
            intent = {
                "create_post_draft": "create_post",
                "revise_post": "revise_post",
                "create_content_plan": "plan",
                "get_analytics": "analytics",
                "make_report": "report",
                "list_posts": "list_posts",
            }.get(call[0] if call else "", "company")
            if any(word in low for word in ("можно ли", "как подключить", "поддерживает")):
                intent = "integration"
            elif low.startswith("как ") or "что уме" in low or "что это за проект" in low:
                intent = "help"
            elif re.search(
                r"(опубликуй|запланируй|отмени|перенеси).*?(пост|черновик)\s*[№#]?\s*\d+", low
            ):
                intent = "publish"
            return LLMResponse(content=json.dumps({"intent": intent}))
        if task and task.startswith("args_"):
            call = _route(current)
            args = call[1] if call else {}
            return LLMResponse(content=json.dumps(args, ensure_ascii=False))
        if task == "publish_intent":
            post = re.search(r"(?:пост|черновик)\s*[№#]?\s*(\d+)", current, re.I)
            action = (
                "cancel"
                if "отмени" in current.lower()
                else "schedule"
                if any(word in current.lower() for word in ("запланируй", "перенеси"))
                else "publish"
            )
            when = last_user[post.end() :].strip() if post else ""
            return LLMResponse(
                content=json.dumps(
                    {"action": action, "post_id": int(post[1]) if post else None, "when": when},
                    ensure_ascii=False,
                )
            )
        if task == "platform_help":
            return LLMResponse(
                content=last_user.partition("\n\nВопрос:\n")[0].removeprefix("Справка:\n")
            )
        if task in {"result_list_posts", "result_analytics"}:
            tool = "list_posts" if task == "result_list_posts" else "get_analytics"
            return LLMResponse(content=_summarize_tool_result(tool, last_user))
        if task == "write_post":
            return LLMResponse(content=_write_post(last_user))
        if task == "revise_post":
            return LLMResponse(content=_revise_post(last_user))
        if task == "content_plan":
            days = int(_line_value(last_user, "Дней", "7"))
            count = int(_line_value(last_user, "Тем", "5"))
            topics = next_topics(last_user, count, seed=date.today().toordinal())
            items = [
                {"day": index * (days - 1) // (count - 1), "topic": topic, "rubric": rubric}
                for index, (topic, rubric) in enumerate(topics)
            ]
            return LLMResponse(content=json.dumps({"items": items}, ensure_ascii=False))
        if task == "suggest_topic":
            system = _last_content(messages, "system")
            ((topic, _),) = next_topics(system, 1, seed=date.today().toordinal())
            return LLMResponse(content=topic)
        if task == "extract_profile" or json_mode:
            return LLMResponse(content=json.dumps(_extract_profile(last_user), ensure_ascii=False))
        if tools:
            return self._agent_step(messages, {t["function"]["name"] for t in tools})
        return LLMResponse(content="Это ответ mock-LLM. Подключите реальную модель в .env.")

    # ------------------------------------------------------------- agent

    def _agent_step(self, messages: list[Message], available: set[str]) -> LLMResponse:
        last = messages[-1]
        if last.get("role") == "tool":
            tool_name = _find_tool_name(messages, last.get("tool_call_id"))
            return LLMResponse(content=_summarize_tool_result(tool_name, last.get("content") or ""))

        text = (last.get("content") or "").strip()
        call = _route(text)
        if call is None or call[0] not in available:
            return LLMResponse(content=_help_answer())
        self._call_counter += 1
        return LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(id=f"call_mock_{self._call_counter}", name=call[0], arguments=call[1])
            ],
        )


# ----------------------------------------------------------------- routing


def _route(text: str) -> tuple[str, dict[str, Any]] | None:
    low = text.lower().replace("ё", "е")

    post_ref = re.search(r"(?:пост|черновик)\w*\s*(?:№|#|номер)?\s*(\d+)", low)
    if post_ref and any(w in low for w in REVISE_WORDS):
        return "revise_post", {"post_id": int(post_ref.group(1)), "instruction": text}

    if (
        "контент-план" in low
        or "контент план" in low
        or ("план" in low and ("недел" in low or "месяц" in low))
    ):
        period = "month" if "месяц" in low else "week"
        return "create_content_plan", {"period": period}

    if any(w in low for w in ("аналитик", "статистик", "охват", "метрик")):
        return "get_analytics", {"days": 7}

    if "отчет" in low:
        return "make_report", {"days": 7}

    if "пост" in low and any(w in low for w in ("покажи", "список", "какие", "мои", "сколько")):
        return "list_posts", {}

    if "профил" in low or "что ты знаешь" in low or "о нас знаешь" in low:
        return "get_business_profile", {}

    if any(w in low for w in POST_WORDS):
        return "create_post_draft", {
            "topic": _extract_topic(text),
            "platform": "telegram",
            "with_image": any(w in low for w in IMAGE_WORDS),
        }
    return None


def _extract_topic(text: str) -> str:
    m = re.search(r"\b(?:про|о|об|на тему)\s+(.+)", text, flags=re.IGNORECASE)
    topic = m.group(1) if m else text
    topic = re.sub(r"[,.]?\s*(?:с|и)\s+(?:картинк|фото|изображен)\w*.*$", "", topic, flags=re.I)
    return topic.strip(' .!?"«»') or "новости компании"


def _find_tool_name(messages: list[Message], tool_call_id: str | None) -> str:
    for msg in reversed(messages):
        for tc in msg.get("tool_calls") or []:
            if tc.get("id") == tool_call_id:
                return tc["function"]["name"]
    return ""


def _summarize_tool_result(tool_name: str, content: str) -> str:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = {"raw": content}
    if isinstance(data, dict) and data.get("error"):
        return f"Не получилось выполнить действие: {data['error']}"
    if isinstance(data, dict) and data.get("status") == "in_development":
        return data.get("message", "Эта функция пока в разработке.")

    if tool_name in ("create_post_draft", "revise_post"):
        return (
            f"Готово! Черновик #{data.get('post_id')} отправлен вам на согласование — "
            "выберите действие кнопками под ним."
        )
    if tool_name == "create_content_plan":
        lines = [f"{item['date']} · {item['topic']}" for item in data.get("items", [])]
        return "Контент-план готов. Утвердите его на главной в Mini App.\n" + "\n".join(lines)
    if tool_name == "list_posts":
        posts = data.get("posts", [])
        if not posts:
            return "Постов пока нет. Напишите, например: «напиши пост про новое меню»."
        lines = [f"#{p['id']} · {p['status']} · {p['topic']}" for p in posts]
        return "Ваши последние посты:\n" + "\n".join(lines)
    if tool_name == "get_business_profile":
        if not data.get("filled"):
            return "Профиль бизнеса пока пуст. Запустите /onboarding и расскажите о себе."
        profile = data.get("profile", {})
        return (
            f"Я знаю, что вы — «{profile.get('name')}». {profile.get('description', '')}\n"
            "Полный профиль — командой /profile."
        )
    if tool_name == "update_business_profile":
        return "Обновил профиль бизнеса."
    if tool_name == "make_report" and data.get("text"):
        return str(data["text"])
    if tool_name == "get_analytics":
        return (
            f"За {data.get('period_days', 7)} дн.: опубликовано {data.get('posts_published', 0)}, "
            f"реакций {data.get('total_reactions', 0)}, комментариев {data.get('comments_total', 0)}."
        )
    return "Готово."


def _help_answer() -> str:
    return (
        "Я ваш AI-маркетолог. Могу, например:\n"
        "• «Напиши пост про осеннее меню» (добавьте «с картинкой», если нужна)\n"
        "• «Покажи мои посты»\n"
        "• «Сделай пост #3 короче»\n"
        "• «Что ты знаешь о нашем бизнесе?»"
    )


# ----------------------------------------------------------------- generation


def _line_value(text: str, label: str, default: str = "") -> str:
    m = re.search(rf"^{label}:\s*(.+)$", text, flags=re.MULTILINE)
    return m.group(1).strip() if m else default


def _hashtag(value: str) -> str:
    word = re.sub(r"[^\wа-яА-ЯёЁ]", "", value.split()[0]) if value.split() else "новости"
    return word.lower() or "новости"


def _write_post(user_msg: str) -> str:
    """Пост из библиотеки шаблонов (app/llm/post_templates.py) по теме и названию компании."""
    topic = _line_value(user_msg, "Тема", "новости компании")
    business = _line_value(user_msg, "Название бизнеса", "Мы")
    return render_post(topic, business)


_EMOJI_RE = re.compile("[\U0001f300-\U0001faff☀-➿]️?")


def _revise_post(user_msg: str) -> str:
    m = re.search(r"<<<\n?(.*?)\n?>>>", user_msg, flags=re.DOTALL)
    text = m.group(1).strip() if m else ""
    instruction = _line_value(user_msg, "Правка клиента").lower()
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    changed = False

    if any(w in instruction for w in ("короч", "сократ", "кратк")) and len(paragraphs) > 2:
        paragraphs = [paragraphs[0], paragraphs[1], paragraphs[-1]]
        changed = True
    if any(w in instruction for w in ("подробн", "длинн", "больше текст")):
        paragraphs.insert(-1, "Мы постоянно развиваемся и всегда рады обратной связи от вас.")
        changed = True
    if any(w in instruction for w in ("официальн", "без эмодзи", "убери эмодзи", "строже")):
        paragraphs = [_EMOJI_RE.sub("", p).strip() for p in paragraphs]
        changed = True
    elif "эмодзи" in instruction or "смайл" in instruction:
        icons = ["🔥", "💛", "👉", "🎉", "✅"]
        paragraphs = [f"{icons[i % len(icons)]} {p}" for i, p in enumerate(paragraphs)]
        changed = True
    if not changed and instruction:
        paragraphs.insert(-1, f"(Учтена правка: {instruction})")
    return "\n\n".join(paragraphs)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _extract_profile(user_msg: str) -> dict[str, Any]:
    text = user_msg.split("\n", 1)[1] if user_msg.startswith("Материалы") else user_msg
    sentences = _sentences(text)
    low_sentences = [s.lower() for s in sentences]

    name = "Мой бизнес"
    m = re.search(r"[«\"]([^»\"]{2,60})[»\"]", text) or re.search(
        r"(?:называ\w*|название\s*[:\-—]?)\s*([А-ЯA-Z][^.,\n]{1,60})", text
    )
    if m:
        name = m.group(1).strip()

    def find(*keys: str, default: str = "") -> str:
        for s, low in zip(sentences, low_sentences, strict=True):
            if any(k in low for k in keys):
                return s
        return default

    products: list[str] = [
        line.strip(" -•*\t") for line in text.splitlines() if line.strip()[:1] in ("-", "•", "*")
    ]
    if not products:
        line = find("предлага", "продаем", "продаём", "услуг", "в меню", "ассортимент")
        if ":" in line:
            line = line.split(":", 1)[1]
        products = [p.strip(" .") for p in re.split(r",| и ", line) if p.strip(" .")][:8]

    restrictions = [
        s
        for s, low in zip(sentences, low_sentences, strict=True)
        if any(k in low for k in ("нельзя", "не упомина", "запрещ", "не использ", "не пиши"))
    ]
    return {
        "name": name,
        "description": " ".join(sentences[:2])[:400] if sentences else "",
        "products": products[:8],
        "audience": find("аудитор", "клиент", "для кого", "гост", default="Местные жители"),
        "usp": find(
            "уникальн", "отлича", "лучш", "единствен", default="Качество и внимание к деталям"
        ),
        "tone": "дружелюбный, на «ты»" if "на ты" in text.lower() else "дружелюбный, на «вы»",
        "restrictions": restrictions,
    }


def _last_content(messages: list[Message], role: str) -> str:
    for msg in reversed(messages):
        if msg.get("role") == role:
            return msg.get("content") or ""
    return ""
