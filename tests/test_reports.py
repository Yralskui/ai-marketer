"""Реакции, сводка и недельный отчёт с выводами (п. 3.6 ТЗ)."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from app.db.models import Comment, CommentStatus, Platform
from app.db.repositories import CommentRepo, PostMetricRepo
from app.llm.base import LLMProvider, LLMResponse
from app.scheduler.jobs import weekly_report
from app.scheduler.setup import create_scheduler
from app.services import accounts, analytics, approval, posts, reports
from app.services.publishing import PublishingService
from tests.conftest import FakePublisher

TZ = ZoneInfo("Asia/Yekaterinburg")


async def _published(session, owner, llm, images, topic: str, *, with_image: bool = False):
    user, business = owner
    service = PublishingService({Platform.TELEGRAM: FakePublisher()})
    post = await posts.create_draft(session, llm, images, business, topic, with_image=with_image)
    await approval.submit_for_approval(session, post, None, None)
    outcome = await approval.approve_and_publish(session, post, user, service)
    assert outcome.ok
    return post


async def test_reactions_are_attached_to_our_post(session, owner, llm, images):
    post = await _published(session, owner, llm, images, "осеннее меню")
    # FakePublisher возвращает external_id вида "msg-<id>"; канал из .env — @test_channel
    assert await analytics.record_reactions(
        session, -100123, "test_channel", post.external_id, {"👍": 3, "🔥": 2}
    )
    metric = await PostMetricRepo(session).get_for_post(post.id)
    assert metric.reactions_total == 5 and metric.reactions == {"👍": 3, "🔥": 2}

    # Обновление заменяет счётчик, а не прибавляет
    await analytics.record_reactions(session, -100123, "test_channel", post.external_id, {"👍": 4})
    assert (await PostMetricRepo(session).get_for_post(post.id)).reactions_total == 4

    # Чужой канал и чужое сообщение не трогаем
    assert not await analytics.record_reactions(session, -100999, "other", post.external_id, {})
    assert not await analytics.record_reactions(session, -100123, "test_channel", "msg-0", {})


async def test_summary_counts_everything(session, owner, llm, images):
    _, business = owner
    p1 = await _published(session, owner, llm, images, "кофе", with_image=True)
    p2 = await _published(session, owner, llm, images, "десерт")
    await PostMetricRepo(session).set_reactions(p1.id, business.id, {"👍": 8})
    await PostMetricRepo(session).set_reactions(p2.id, business.id, {"👍": 2})
    for status in (CommentStatus.SENT, CommentStatus.PENDING):
        await CommentRepo(session).add(
            Comment(business_id=business.id, post_id=p1.id, chat_id=1, message_id=1,
                    text="круто", status=status)
        )  # fmt: skip

    s = await analytics.get_summary(session, business, days=7, tz=TZ)
    assert s.posts_published == 2
    assert s.total_reactions == 10 and s.avg_reactions == 5
    assert s.comments_total == 2 and s.comments_replied == 1
    assert s.top_posts[0]["id"] == p1.id
    assert s.with_image_avg == 8 and s.without_image_avg == 2
    assert s.approved == 2 and s.drafts_created == 2


async def test_report_has_numbers_conclusions_and_actions(session, owner, llm, images, tmp_path):
    _, business = owner
    p1 = await _published(session, owner, llm, images, "кофе", with_image=True)
    await _published(session, owner, llm, images, "десерт")
    await PostMetricRepo(session).set_reactions(p1.id, business.id, {"👍": 8})

    report = await reports.build_report(session, llm, business, days=7, tz=TZ, out_dir=tmp_path)
    assert "Отчёт за 7 дн." in report.text
    assert "Опубликовано: <b>2</b>" in report.text and "Реакции: <b>8</b>" in report.text
    assert "<b>Выводы</b>" in report.text and "<b>Что сделать</b>" in report.text
    assert any("картинк" in a for a in report.insights.actions)
    assert report.chart_path and report.chart_path.exists()


async def test_empty_period_still_gives_advice(session, owner, llm, tmp_path):
    _, business = owner
    report = await reports.build_report(session, llm, business, out_dir=tmp_path)
    assert "Опубликовано: <b>0</b>" in report.text
    assert "простаивает" in report.text


class InsightLLM(LLMProvider):
    name = "openai"

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        return LLMResponse(
            content='{"conclusions": ["Вывод от модели"], "hypotheses": ["Гипотеза"],'
            ' "actions": ["Действие"]}'
        )


async def test_llm_insights_are_used_when_available(session, owner, tmp_path):
    _, business = owner
    report = await reports.build_report(session, InsightLLM(), business, out_dir=tmp_path)
    assert report.insights.conclusions == ["Вывод от модели"]
    assert "Действие" in report.text


async def test_weekly_job_sends_report_to_each_company(db, session, owner, llm, tmp_path):
    admin, _ = owner
    await accounts.create_company(session, admin, "Вторая")
    await session.commit()
    sent: list[tuple[int, str]] = []

    async def sender(chat_id: int, report: reports.Report) -> None:
        sent.append((chat_id, report.summary.company))

    count = await weekly_report(db.sessionmaker, llm, sender, tz=TZ, out_dir=tmp_path)
    assert count == 2
    assert {company for _, company in sent} == {"Кофейня «Зерно»", "Вторая"}


async def test_report_interval_for_demo(settings):
    from app.container import build_container

    settings.enable_weekly_report = True
    settings.report_interval_minutes = 5
    scheduler = create_scheduler(build_container(settings))
    job = scheduler.get_job("weekly_report")
    assert job is not None and "interval" in str(job.trigger)


async def test_social_counters_are_scoped_to_original_channel(db, session, owner, llm, images):
    from unittest.mock import AsyncMock

    from app.db.repositories import ChannelRepo
    from app.publishers.analytics import AnalyticsError, ChannelMetrics, PostMetrics
    from app.services.social_metrics import collect_hour

    _, business = owner
    p1 = await _published(session, owner, llm, images, "Старый канал")
    await ChannelRepo(session).upsert(business.id, Platform.TELEGRAM, "@current")
    p2 = await _published(session, owner, llm, images, "Новый канал")
    # Telegram может выдать одинаковые ID сообщений в разных каналах.
    p1.external_id = p2.external_id = "14"
    dry = await _published(session, owner, llm, images, "Симуляция")
    dry.external_id = "dry-run-99"
    await session.flush()

    async def read_posts(channel_id, ids):
        assert ids == ["14"]
        if channel_id == "@current":
            raise AnalyticsError("временный сбой")
        assert channel_id == "@test_channel"
        return {"14": PostMetrics(0, 3), "99": PostMetrics(99, 99)}

    source = AsyncMock()
    source.channel_metrics.return_value = ChannelMetrics(42)
    source.post_metrics.side_effect = read_posts
    await session.commit()
    await collect_hour(db.sessionmaker, {Platform.TELEGRAM: source})
    summary = await analytics.get_summary(session, business)

    source.channel_metrics.assert_awaited_once_with("@current")
    assert source.post_metrics.await_count == 2
    assert summary.subscribers == {"telegram": 42}
    assert summary.total_views == 0 and summary.posts_with_views == 1
    assert {stat.id: stat.views for stat in summary.posts} == {p1.id: 0, p2.id: None, dry.id: None}
    assert {stat.id: stat.forwards for stat in summary.posts}[p1.id] == 3
    assert "Счётчики просмотров доступны только для части" in summary.limitations
