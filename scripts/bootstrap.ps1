# Первичная настройка на Windows: venv, зависимости, .env, проверка.
# Запуск из корня проекта:  powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    py -3 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Создан .env из .env.example - заполните BOT_TOKEN, ALLOWED_USER_IDS, TELEGRAM_CHANNEL_ID"
}

& .\.venv\Scripts\python.exe -m app.main --check
