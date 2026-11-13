# Образ бота AI-маркетолога. Сборка: docker compose build
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Шрифт с кириллицей для картинок-заглушек (app/images/mock_provider.py)
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Без root. Папка data принадлежит пользователю приложения, поэтому именованный
# volume при первом запуске получит те же права.
RUN useradd --create-home --uid 1000 marketer \
    && mkdir -p /app/data/images \
    && chown -R marketer:marketer /app/data
USER marketer

VOLUME ["/app/data"]

CMD ["python", "-m", "app.main"]
