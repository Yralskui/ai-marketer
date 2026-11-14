"""Проверка подписанного Telegram initData."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from pydantic import BaseModel, Field


class TelegramIdentity(BaseModel):
    id: int = Field(gt=0, le=2**52 - 1)
    first_name: str = ""
    last_name: str = ""
    photo_url: str = ""

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


def validate_init_data(value: str, bot_token: str) -> TelegramIdentity:
    if not bot_token or len(value) > 8192:
        raise ValueError("Invalid Telegram authorization")
    pairs = parse_qsl(value, strict_parsing=True, max_num_fields=30)
    data = dict(pairs)
    if len(pairs) != len(data):
        raise ValueError("Invalid Telegram authorization")
    received = data.pop("hash", "")
    payload = "\n".join(f"{key}={item}" for key, item in sorted(data.items()))
    secret = hmac.digest(b"WebAppData", bot_token.encode(), hashlib.sha256)
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    if not received.isascii() or not hmac.compare_digest(received, expected):
        raise ValueError("Invalid Telegram authorization")
    age = time.time() - int(data.get("auth_date", "0"))
    if not -30 <= age <= 12 * 3600:
        raise ValueError("Open the app again in Telegram")
    return TelegramIdentity.model_validate(json.loads(data["user"]))
