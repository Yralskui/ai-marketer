from datetime import timedelta
from unittest.mock import AsyncMock

from app.db.models import Platform, Post, PostStatus
from app.db.repositories import ChannelRepo
from app.publishers.analytics import ChannelMetrics, PostMetrics
from app.services import accounts, social_metrics
from app.utils.time import utcnow


async def test_hourly_collection_keeps_history_and_stops_reading_deleted_posts(db, session, owner):
    user, company = owner
    channel = await ChannelRepo(session).get_active(company.id, Platform.TELEGRAM)
    second = await accounts.create_company(session, user, "Вторая")
    await ChannelRepo(session).upsert(second.id, Platform.TELEGRAM, "@other")
    start = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    post = Post(
        business_id=company.id,
        channel_id=channel.id,
        platform=Platform.TELEGRAM,
        text="Опубликован",
        status=PostStatus.PUBLISHED,
        external_id="10",
        published_at=start,
    )
    session.add(post)
    await session.commit()

    source = AsyncMock()
    source.channel_metrics.side_effect = lambda channel_id: ChannelMetrics(
        10 if channel_id == channel.external_id else 80
    )
    source.post_metrics.side_effect = [{"10": PostMetrics(0, 0)}, {"10": PostMetrics(deleted=True)}]
    sources = {Platform.TELEGRAM: source}
    await social_metrics.collect_hour(db.sessionmaker, sources, start)
    await social_metrics.collect_hour(db.sessionmaker, sources, start + timedelta(minutes=1))
    await social_metrics.collect_hour(db.sessionmaker, sources, start + timedelta(hours=1))
    await social_metrics.collect_hour(db.sessionmaker, sources, start + timedelta(hours=2))
    assert source.post_metrics.await_count == 2
    assert source.channel_metrics.await_count == 6

    first = await social_metrics.series(session, company.id, start, utcnow(), post.id)
    assert [point["views"] for point in first["points"]] == [0, None]
    assert first["points"][1]["deleted"]
    assert len(first["channels"][0]["points"]) == 3
    assert first["channels"][0]["points"][0][1] == 10
    other = await social_metrics.series(session, second.id, start, utcnow(), post.id)
    assert other["points"] == []
    assert other["channels"][0]["points"][0][1] == 80
