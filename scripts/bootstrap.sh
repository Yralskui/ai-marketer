#!/usr/bin/env bash
# Первичная настройка на macOS/Linux: venv, зависимости, .env, проверка.
# Запуск из корня проекта:  bash scripts/bootstrap.sh
set -euo pipefail

PYTHON="${PYTHON:-python3}"
[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан .env из .env.example — заполните BOT_TOKEN, ALLOWED_USER_IDS, TELEGRAM_CHANNEL_ID"
fi

.venv/bin/python -m app.main --check
