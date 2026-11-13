"""Репозитории: по файлу на сущность. Только доступ к данным, без бизнес-логики."""

from app.db.repositories.agent_message import AgentMessageRepo
from app.db.repositories.business import BusinessRepo
from app.db.repositories.channel import ChannelRepo
from app.db.repositories.comment import CommentRepo
from app.db.repositories.content_plan import ContentPlanRepo
from app.db.repositories.event_log import EventLogRepo
from app.db.repositories.post import PostRepo
from app.db.repositories.post_metric import PostMetricRepo
from app.db.repositories.source import SourceRepo
from app.db.repositories.user import UserRepo

__all__ = [
    "AgentMessageRepo",
    "BusinessRepo",
    "ChannelRepo",
    "CommentRepo",
    "ContentPlanRepo",
    "EventLogRepo",
    "PostMetricRepo",
    "PostRepo",
    "SourceRepo",
    "UserRepo",
]
