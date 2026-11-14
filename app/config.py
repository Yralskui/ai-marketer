"""Конфигурация приложения: читается из переменных окружения и файла `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"


def parse_vk_communities(value: str) -> dict[int, str]:
    """«123:ключ,456:ключ» → {123: «ключ», 456: «ключ»}. Ключи в сообщениях об ошибках не пишем."""
    tokens: dict[int, str] = {}
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        group, separator, token = entry.partition(":")
        group = group.strip().lstrip("-")
        if not separator or not group.isdecimal() or not token.strip():
            raise ValueError("VK_COMMUNITIES: ожидается «<id сообщества>:<ключ>», через запятую")
        tokens[int(group)] = token.strip()
    return tokens


class Settings(BaseSettings):
    """Все настройки проекта. Описание каждой переменной — в `.env.example`."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Пустое значение в .env = значение по умолчанию (удобно для .env.example)
        env_ignore_empty=True,
    )

    # --- Telegram ---
    bot_token: str = ""
    # Строка вида "123,456": pydantic-settings иначе ждёт JSON-список
    allowed_user_ids: str = ""
    telegram_channel_id: str = ""
    # socks5://user:pass@host:port или http://… — если api.telegram.org недоступен (VPS в РФ)
    telegram_proxy: str = ""
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    upload_public_url: str = ""
    miniapp_public_url: str = ""
    miniapp_configure_menu: bool = True
    miniapp_open_signup: bool = False
    default_profile_file: str = ""
    miniapp_stories_file: str = ""

    # --- LLM ---
    llm_provider: str = "mock"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_model_light: str = ""
    llm_model_heavy: str = ""
    embedding_model: str = "text-embedding-3-small"
    llm_timeout: float = 60.0
    llm_max_retries: int = 3
    llm_temperature: float = 0.7

    # --- Изображения ---
    image_provider: str = "mock"
    image_api_key: str = ""
    image_model: str = "gpt-image-2.5-flare"
    image_timeout: float = 60.0

    # --- Учёт расходов (цены в рублях; mock-провайдеры бесплатны) ---
    llm_price_input_per_1m: float = 15.0
    llm_price_output_per_1m: float = 60.0
    image_price: float = 4.0

    # --- ВКонтакте ---
    # Сообщества и ключи доступа: «<id сообщества>:<ключ>», несколько — через запятую.
    # Ключи — секреты, хранятся только в .env; сообщество компании выбирается в Mini App.
    vk_communities: str = ""
    vk_api_version: str = "5.199"
    vk_login: str = ""
    vk_apikey: SecretStr = SecretStr("")
    vk_password: SecretStr = SecretStr("")
    # Бизнес-аккаунт ВК с подтверждённым профилем и токеном администратора: картинки, правка
    # записей, просмотры. False — ключ сообщества: эти функции отключены, ошибок они не дают
    vk_is_business_account: bool = Field(
        False, validation_alias=AliasChoices("IS_BUSINESS_ACCOUNT", "vk_is_business_account")
    )

    @property
    def vk_tokens(self) -> dict[int, str]:
        tokens = parse_vk_communities(self.vk_communities)
        token = self.vk_apikey.get_secret_value() or self.vk_password.get_secret_value()
        if token and self.vk_login:
            address = self.vk_login.strip()
            url = urlsplit(address if "://" in address else "https://" + address)
            path = url.path.strip("/") if url.hostname in {"vk.ru", "vk.com"} else address
            group = path.removeprefix("club").removeprefix("public").lstrip("-")
            if not group.isascii() or not group.isdecimal() or int(group) <= 0:
                raise ValueError("VK_LOGIN: укажите ID сообщества или ссылку vk.ru/club<ID>")
            tokens[int(group)] = token
        return tokens

    @field_validator("vk_communities")
    @classmethod
    def _check_vk_communities(cls, value: str) -> str:
        parse_vk_communities(value)  # неверный формат — сразу понятная ошибка при запуске
        return value

    # --- Хранилище ---
    database_url: str = ""
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "ai-marketer"

    # --- Поведение ---
    timezone: str = "Asia/Yekaterinburg"
    publish_dry_run: bool = False
    enable_auto_drafts: bool = False
    auto_drafts_interval_minutes: int = 0
    enable_weekly_report: bool = False
    publish_check_interval_seconds: int = 60
    # 0 — отчёт по понедельникам в 10:00; N > 0 — каждые N минут (для демо автономности)
    report_interval_minutes: int = 0
    log_level: str = "INFO"

    # --- Агент ---
    agent_max_steps: int = 6
    agent_history_limit: int = 20

    @field_validator("llm_provider", "image_provider")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("upload_public_url", "miniapp_public_url")
    @classmethod
    def _upload_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        url = urlsplit(value)
        local = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}
        if not (local or url.scheme == "https") or not url.netloc or not url.hostname:
            raise ValueError("UPLOAD_PUBLIC_URL требует HTTPS (HTTP допускается для localhost)")
        if (
            url.username is not None
            or url.password is not None
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError("UPLOAD_PUBLIC_URL должен содержать только схему, хост и порт")
        return value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # бросит исключение на неизвестный пояс
        return value

    @property
    def telegram_miniapp_url(self) -> str | None:
        if self.miniapp_public_url.startswith("https://"):
            return f"{self.miniapp_public_url}/miniapp"
        return None

    @property
    def allowed_ids(self) -> set[int]:
        """Множество Telegram ID, которым разрешён доступ к боту."""
        result: set[int] = set()
        for part in self.allowed_user_ids.replace(";", ",").split(","):
            part = part.strip()
            if part:
                result.add(int(part))
        return result

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def effective_database_url(self) -> str:
        """URL БД; по умолчанию — SQLite-файл `data/app.db` в корне проекта."""
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{(DATA_DIR / 'app.db').as_posix()}"


@lru_cache
def get_settings() -> Settings:
    """Единый экземпляр настроек (кэшируется)."""
    return Settings()
