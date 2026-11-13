"""Конфигурация приложения: читается из переменных окружения и файла `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"


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

    # --- LLM ---
    llm_provider: str = "mock"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout: float = 60.0
    llm_max_retries: int = 3
    llm_temperature: float = 0.7

    # --- Изображения ---
    image_provider: str = "mock"
    image_api_key: str = ""

    # --- Учёт расходов (цены в рублях; mock-провайдеры бесплатны) ---
    llm_price_input_per_1m: float = 15.0
    llm_price_output_per_1m: float = 60.0
    image_price: float = 4.0

    # --- Хранилище ---
    database_url: str = ""

    # --- Поведение ---
    timezone: str = "Asia/Yekaterinburg"
    publish_dry_run: bool = False
    enable_auto_drafts: bool = False
    enable_weekly_report: bool = False
    publish_check_interval_seconds: int = 60
    log_level: str = "INFO"

    # --- Агент ---
    agent_max_steps: int = 6
    agent_history_limit: int = 20

    @field_validator("llm_provider", "image_provider")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # бросит исключение на неизвестный пояс
        return value

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
