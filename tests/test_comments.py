"""Ответы на комментарии: черновик, согласование, автоответ по разрешению (п. 3.4 ТЗ)."""

from __future__ import annotations

import pytest

from app.db.models import Comment, CommentStatus
from app.llm.base import LLMProvider, LLMResponse
from app.services import accounts, comments
from app.services.comments import CommentActionError


class FakeReplier:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[int, int, str]] = []

    async def reply(self, chat_id: int, message_id: int, text: str) -> int:
        if self.fail:
            raise RuntimeError("нет прав в группе")
        self.sent.append((chat_id, message_id, text))
        return 9000 + len(self.sent)


class JsonLLM(LLMProvider):
    name = "openai"

    def __init__(self, content: str) -> None:
        self.content = content

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        return LLMResponse(content=self.content)


def _comment(text: str, author: str = "Анна") -> Comment:
    return Comment(chat_id=-100555, message_id=42, author_id=777, author_name=author, text=text)


async def test_without_permission_reply_waits_for_approval(session, owner, llm):
    _, business = owner
    replier = FakeReplier()
    comment = await comments.process_comment(
        session, llm, business, _comment("Классный пост! Когда новинки?"), None, replier
    )
    assert comment.status == CommentStatus.PENDING
    assert "Анна" in comment.reply_text
    assert replier.sent == []  # без разрешения ничего не уходит


async def test_auto_reply_sends_ordinary_comment(session, owner, llm):
    _, business = owner
    comments.set_auto_reply(business, True)
    replier = FakeReplier()
    comment = await comments.process_comment(
        session, llm, business, _comment("Спасибо, очень вкусно!"), None, replier
    )
    assert comment.status == CommentStatus.SENT and comment.auto_replied
    assert replier.sent == [(-100555, 42, comment.reply_text)]
    assert comment.reply_message_id == 9001


@pytest.mark.parametrize(
    "text",
    ["Ужасное обслуживание, больше не приду", "Верните деньги за заказ", "Где мой заказ?"],
)
async def test_complaints_always_go_to_human(session, owner, llm, text):
    _, business = owner
    comments.set_auto_reply(business, True)
    replier = FakeReplier()
    comment = await comments.process_comment(session, llm, business, _comment(text), None, replier)
    assert comment.status == CommentStatus.PENDING
    assert comment.needs_human_reason
    assert replier.sent == []


async def test_reply_is_sent_once(session, owner, llm, settings):
    user, business = owner
    replier = FakeReplier()
    comment = await comments.process_comment(
        session, llm, business, _comment("Отличная идея"), None, replier
    )
    await comments.send_reply(session, comment, comment.reply_text, replier, user=user)
    with pytest.raises(CommentActionError):
        await comments.send_reply(session, comment, comment.reply_text, replier, user=user)
    assert len(replier.sent) == 1


async def test_failed_reply_can_be_retried(session, owner, llm):
    user, business = owner
    comment = await comments.process_comment(
        session, llm, business, _comment("Отличная идея"), None, FakeReplier()
    )
    with pytest.raises(RuntimeError):
        await comments.send_reply(session, comment, "Спасибо!", FakeReplier(fail=True), user=user)
    assert comment.status == CommentStatus.PENDING
    ok = FakeReplier()
    await comments.send_reply(session, comment, "Спасибо!", ok, user=user)
    assert comment.status == CommentStatus.SENT and ok.sent


async def test_llm_decision_and_safety_net(owner):
    _, business = owner
    calm = JsonLLM('{"reply": "Спасибо, Анна!", "needs_human": false, "reason": ""}')
    draft = await comments.draft_reply(calm, business, _comment("Супер"), None)
    assert draft.text == "Спасибо, Анна!" and not draft.needs_human

    # LLM пропустила жалобу — эвристика всё равно отдаёт её человеку
    draft = await comments.draft_reply(calm, business, _comment("Это обман!"), None)
    assert draft.needs_human and draft.reason == "негатив"

    broken = JsonLLM("просто текст без JSON")
    draft = await comments.draft_reply(broken, business, _comment("Супер"), None)
    assert draft.needs_human


async def test_group_is_bound_to_company_by_channel(session, owner):
    admin, company_a = owner
    company_b = await accounts.create_company(session, admin, "Вторая")
    await accounts.set_channel(session, company_b, "@second_channel", admin)

    found = await comments.business_for_channel(session, -100999, "second_channel")
    assert found is not None and found[0].id == company_b.id
    comments.set_discussion_chat(found[0], -100777)
    assert (await comments.business_for_group(session, -100777)).id == company_b.id
    assert await comments.business_for_group(session, -100778) is None
    assert comments.discussion_chat_id(company_a) is None
