"""Инструменты агента. Импорт модулей регистрирует инструменты в default_registry.

Новый инструмент: функция с @tool в подходящем файле этой папки (или новый файл +
строка импорта ниже). Ядро агента править не нужно.
"""

from app.agent.tools import (  # noqa: F401
    analytics_tools,
    plan_tools,
    post_tools,
    profile_tools,
)
