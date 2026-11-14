"""Генерация изображений за единым интерфейсом (app/images/base.py)."""


class ImageGenerationError(RuntimeError):
    """Провайдер не смог вернуть готовое изображение."""
