from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import types

from app.publishers.analytics import ChannelMetrics, PostMetrics
from app.publishers.telegram_analytics import TelegramAnalytics


async def test_subscribers_use_bot_api_without_mtproto():
    bot = SimpleNamespace(get_chat_member_count=AsyncMock(return_value=42))
    source = TelegramAnalytics(bot)
    assert await source.channel_metrics("-1001234567890") == ChannelMetrics(42)
    bot.get_chat_member_count.assert_awaited_once_with(-1001234567890, request_timeout=5)
    assert await source.post_metrics("-1001234567890", ["1"]) == {}


async def test_mtproto_reads_existing_counters_in_batches():
    bot = SimpleNamespace(token="123456:fake")
    source = TelegramAnalytics(bot)

    async def reply(request):
        return SimpleNamespace(
            messages=[
                types.Message(
                    id=item.id, peer_id=types.PeerChannel(1234567890), views=0, forwards=2
                )
                if item.id == 1
                else types.MessageEmpty(item.id, None)
                for item in request.id
            ]
        )

    client = AsyncMock(side_effect=reply)
    client.is_connected = lambda: False
    source.client = client
    result = await source.post_metrics(
        "-1001234567890", [str(i) for i in range(1, 102)] + ["dry-run-103"]
    )
    assert result["1"] == PostMetrics(0, 2)
    assert result["101"] == PostMetrics(deleted=True)
    assert "dry-run-103" not in result
    client.start.assert_awaited_once_with(bot_token=bot.token)
    requests = [call.args[0] for call in client.await_args_list]
    assert [len(request.id) for request in requests] == [100, 1]
    assert requests[0].channel.channel_id == 1234567890
