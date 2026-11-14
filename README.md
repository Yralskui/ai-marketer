# Автономный AI-маркетолог

Прототип AI-маркетолога, который работает как SMM-менеджер почти без участия человека.
Он изучает бизнес по рассказу, документам и сайту, готовит посты и картинки и отправляет их
клиенту на согласование в Telegram. После одобрения пост выходит в Telegram-канал, сразу или
по расписанию.

**Стек:** Python 3.11+, aiogram 3, aiohttp, SQLAlchemy 2 (async), PostgreSQL с pgvector,
S3, APScheduler и OpenAI Responses API. Для локальных тестов поддерживается SQLite.

Документация: [Telegram Mini App](docs/MINIAPP.md) · [архитектура](docs/ARCHITECTURE.md) · [бэклог](docs/TASKS.md) ·
[подключение компании](docs/ONBOARDING_COMPANY.md) ·
[выбор нейросети](docs/AI_OPTIONS.md) · [функции и ограничения](docs/FEATURES.md) ·
[доработки](docs/ROADMAP.md) · [затраты](docs/COSTS.md) ·
[сценарий демо](docs/DEMO.md) · [чек-лист функций](docs/FEATURE_CHECKLIST.md) ·
[данные Салаватстекла](content/salavatsteklo/README.md) ·
[решения](docs/DECISIONS.md) · [правила для Claude Code](CLAUDE.md)

---

## Быстрый старт

Для запуска всех сервисов используйте [Docker Compose](docs/MINIAPP.md#запуск).
Для локальной разработки нужен Python 3.11 или новее.

### Windows (PowerShell)

```powershell
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements-dev.txt
copy .env.example .env
notepad .env
python -m app.main --check
python -m app.main
```

Или одной командой: `powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1`.

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
nano .env
python -m app.main --check
python -m app.main
```

Или одной командой: `bash scripts/bootstrap.sh`.

`python -m app.main --check` инициализирует конфиг, БД, агента и планировщик без подключения к
Telegram и подсказывает, чего не хватает в `.env`.

---

## Настройка Telegram

1. **Создать бота.** Напишите [@BotFather](https://t.me/BotFather) → `/newbot` → придумайте имя
   и username, который заканчивается на `bot`. Скопируйте токен в `BOT_TOKEN`.
2. **Узнать свой Telegram ID.** Напишите [@userinfobot](https://t.me/userinfobot). Есть и
   второй способ: запустите нашего бота с пустым `ALLOWED_USER_IDS` и напишите ему `/start`, он
   ответит вашим ID. Впишите ID в `ALLOWED_USER_IDS` (несколько — через запятую: ваш и
   напарника).
3. **Создать канал** (публичный или приватный) и **сделать бота администратором**: канал →
   Управление каналом → Администраторы → Добавить → найдите бота по username → включите
   «Публикация сообщений».
4. **ID канала** — в `TELEGRAM_CHANNEL_ID`:
   - публичный канал: `@username_канала`;
   - приватный канал: число вида `-100XXXXXXXXXX`. Его можно узнать, переслав любой пост из
     канала боту [@userinfobot](https://t.me/userinfobot) или
     [@getmyid_bot](https://t.me/getmyid_bot). Ещё вариант: открыть канал в web.telegram.org —
     в адресе будет `#-100…`.
5. Откройте Mini App через `/start`, выберите компанию и проверьте права бота в диагностике канала.
6. **Оформление.** Текст описания и аватарку (`app/bot/assets/avatar.png`) бот ставит сам при
   запуске; аватарку — только если её ещё нет. Картинку над описанием в пустом чате Bot API
   ставить не умеет: [@BotFather](https://t.me/BotFather) → `/mybots` → бот → Edit Bot →
   **Edit Description Picture** → `app/bot/assets/description.png`.

   Перерисовать картинки: `python scripts/make_brand_assets.py`.

## Режимы работы

| Режим | Что поставить в `.env` | Что происходит |
|---|---|---|
| **Разработка без денег и интернета для LLM** | `LLM_PROVIDER=mock`, `IMAGE_PROVIDER=mock`, `PUBLISH_DRY_RUN=true` | Агент и тексты — детерминированные заглушки, картинки рисует Pillow, публикация симулируется, канал не получает сообщения |
| **Проверка канала** | то же, но `PUBLISH_DRY_RUN=false` | Заглушечные посты реально уходят в канал |
| **OpenAI** | `LLM_PROVIDER=openai`, `LLM_API_KEY=…`, `LLM_MODEL_LIGHT=gpt-6-luna`, `LLM_MODEL_HEAVY=gpt-6-luna`, `IMAGE_PROVIDER=openai`, `IMAGE_MODEL=gpt-image-2.5-flare` | OpenAI пишет тексты и генерирует иллюстрации по одному ключу. Флаг `PUBLISH_DRY_RUN` управляет отправкой постов в канал |

Остальные переменные описаны в [.env.example](.env.example).

## Telegram-бот и Mini App

- `/start` открывает Mini App и сбрасывает текущий диалог. В личном чате можно написать
  обычным текстом, например «Подготовь пост про стеклянные витрины на завтра в 15:00».
  Бот уточнит компанию и недостающие параметры. Предложение появится в Mini App.
- В Mini App можно настроить профили и материалы, вести контент-планы и согласовывать
  публикации. Там же доступны расписание, история, отчёты, диагностика, расходы, фотобанк,
  приглашения и согласование ответов на комментарии.

[Инструкция по Mini App](docs/MINIAPP.md)

## Тесты и линтер

```bash
pytest
ruff check .
ruff format --check .
```

Исправить замечания автоматически: `ruff check --fix . && ruff format .`.
Тесты работают без сети и ключей: in-memory SQLite, mock-LLM и фейковая сессия Telegram.

---

## Локальный запуск на macOS в Docker

1. Запустите Docker Desktop и включите Happ. Бот работает через long polling, поэтому
   для сообщений Telegram входящий порт на Mac не нужен. Используйте отдельного тестового бота BotFather.
2. Сохраните обычный текстовый UTF-8 файл `ai-marketer.env` на Рабочем столе. Формат —
   одна строка `ИМЯ=значение`, без `export` и без расширения `.txt`.
   Для первого теста используйте `PUBLISH_DRY_RUN=true`. Оставьте `TELEGRAM_PROXY`
   пустым, если контейнер уже видит Telegram через сеть Mac. Настройки — в
   [шаблоне](.env.example) и [инструкции OpenAI](docs/AI_OPTIONS.md).
3. Поместите файл в репозиторий под именем `.env`, заполните настройки PostgreSQL, S3
   и публичного HTTPS-адреса по [инструкции Mini App](docs/MINIAPP.md#запуск).

   ```bash
   cd /путь/к/ai-marketer
   if [ ! -e .env ]; then install -m 600 "$HOME/Desktop/ai-marketer.env" .env; fi
   docker compose -f docker-compose.yml -f docker-compose.miniapp.yml up -d --build
   docker compose -f docker-compose.yml -f docker-compose.miniapp.yml ps
   curl -fsS http://localhost:8080/readyz
   ```

   Compose получает переменные из `.env`. Этот файл исключён из Git и контекста сборки.

4. Отправьте боту `/start` и откройте Mini App. Выберите профиль, добавьте материал и
   попросите бота подготовить тестовый пост. В режиме dry-run Mini App симулирует
   публикацию. Для реальной отправки после переключения режима создайте свежий черновик.

Docker Desktop отправляет запросы контейнера через сетевой процесс на Mac. Проверка маршрута
с включённым Happ: сравните внешний IP macOS и контейнера, затем проверьте адреса API из
контейнера. Для OpenAI запрос к `/v1/models` без ключа должен вернуть HTTP 401. Для Telegram
`api.telegram.org` достаточно ответа HTTP с редиректом или успешным переходом. Эта проверка
подтверждает доступность адресов. Работу ключей подтверждает только запрос с авторизацией.
[Сеть Docker Desktop](https://docs.docker.com/desktop/features/networking/)

## Деплой на сервер (Docker)

Бот работает 24/7 на сервере, компьютер администратора не нужен. Нужен Linux-сервер с Docker
и плагином `docker compose`.

**Первый запуск:**

```bash
git clone git@github.com:Yralskui/ai-marketer.git /opt/ai-marketer
cd /opt/ai-marketer
cp .env.example .env
nano .env        # боевой BOT_TOKEN, ALLOWED_USER_IDS, ключи LLM, PUBLISH_DRY_RUN=false
docker compose -f docker-compose.yml -f docker-compose.miniapp.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.miniapp.yml logs -f bot worker
```

Репозиторий приватный. Для клонирования на сервере удобнее всего **deploy key**:
`ssh-keygen -t ed25519` на сервере → GitHub → Settings → Deploy keys → Add (только чтение).

**Обновление:**

```bash
cd /opt/ai-marketer
git pull
docker compose -f docker-compose.yml -f docker-compose.miniapp.yml up -d --build
```

Данные Mini App хранятся в PostgreSQL и закрытом S3, оба используют постоянные Docker
volumes. Для резервного копирования нужны дамп PostgreSQL и копия объектов S3.
Настройки HTTPS, воркера и остальных зависимостей описаны в
[инструкции Mini App](docs/MINIAPP.md#запуск).

Один `BOT_TOKEN` нельзя одновременно запускать на сервере и локально: Telegram отдаёт
обновления только одному процессу. Для разработки нужен свой тестовый бот.

---

## Работа в Git вдвоём

- `main` всегда должна запускаться. Каждый работает в своей ветке:
  ```bash
  git switch main
  git pull --rebase origin main
  git switch -c feature/content-plan
  ```
- Коммитим часто и мелко, по [Conventional Commits](https://www.conventionalcommits.org/ru/):
  `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`, `chore: ...`.
- Перед пушем подтягиваем свежий `main` и прогоняем проверки:
  ```bash
  git pull --rebase origin main
  ruff check . && ruff format --check . && pytest && python -m app.main --check
  git push -u origin feature/content-plan
  ```
- Сливаем через Pull Request. Напарник смотрит его хотя бы бегло. Лучше несколько маленьких
  PR, чем один большой.
- Потоки задач разведены по файлам ([docs/TASKS.md](docs/TASKS.md)). Общие интерфейсы
  (`*/base.py`, `db/models.py`, машину состояний) меняем только договорившись.
- Секреты не коммитим: `.env` и `data/` уже в `.gitignore`.

### Подключить напарника к GitHub

1. Создайте **приватный** репозиторий на github.com (без README, `.gitignore` и лицензии —
   они уже есть) или через GitHub CLI: `gh repo create ai-marketer --private --source=. --push`.
2. Если репозиторий создан через сайт:
   ```bash
   git remote add origin https://github.com/<ваш-логин>/ai-marketer.git
   git push -u origin main
   ```
3. Repository → Settings → Collaborators → Add people → логин напарника. Напарник принимает
   приглашение из почты.
4. Напарник выполняет:
   ```bash
   git clone https://github.com/<ваш-логин>/ai-marketer.git
   cd ai-marketer
   ```
   Затем «Быстрый старт» выше. Свой `.env` он заполняет сам: своего тестового бота и свой ID
   (два процесса с одним `BOT_TOKEN` конфликтуют при polling).
5. Рекомендуется: Settings → Branches → защитить `main` (merge только через PR).
