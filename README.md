# Автономный AI-маркетолог

Прототип AI-маркетолога, который работает как SMM-менеджер почти без участия человека.
Он изучает бизнес по рассказу, документам и сайту, готовит посты и картинки и отправляет их
клиенту на согласование в Telegram. После одобрения пост выходит в Telegram-канал, сразу или
по расписанию.

**Стек:** Python 3.11+, aiogram 3, SQLAlchemy 2 (async) + SQLite, APScheduler, OpenAI-совместимый
LLM-клиент, pydantic-settings, ruff, pytest.

Документация: [архитектура](docs/ARCHITECTURE.md) · [бэклог](docs/TASKS.md) ·
[подключение компании](docs/ONBOARDING_COMPANY.md) ·
[решения](docs/DECISIONS.md) · [правила для Claude Code](CLAUDE.md)

---

## Быстрый старт

Нужен Python 3.11 или новее (`python --version`; на Windows — `py -0`).

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
5. Перезапустите бота и выполните `/status`: в строке канала должно быть «✅ бот — администратор».

## Режимы работы

| Режим | Что поставить в `.env` | Что происходит |
|---|---|---|
| **Разработка без денег и интернета для LLM** | `LLM_PROVIDER=mock`, `IMAGE_PROVIDER=mock`, `PUBLISH_DRY_RUN=true` | Агент и тексты — детерминированные заглушки, картинки рисует Pillow, публикация только пишется в лог |
| **Проверка канала** | то же, но `PUBLISH_DRY_RUN=false` | Заглушечные посты реально уходят в канал |
| **Реальный** | `LLM_PROVIDER=openai`, `LLM_API_KEY=…`, `LLM_MODEL=gpt-4o-mini` (при необходимости `LLM_BASE_URL` совместимого сервиса), `PUBLISH_DRY_RUN=false` | Тексты пишет настоящая модель |

Остальные переменные описаны в [.env.example](.env.example).

## Что умеет бот

- `/start`, `/help` — приветствие и подсказки.
- `/onboarding` — рассказать о бизнесе текстом, файлом (PDF, DOCX, TXT) или ссылкой на сайт.
  `/done` или кнопка «Готово» собирает профиль. `/profile` — посмотреть профиль.
- Свободный текст передаётся агенту: «напиши пост про осеннее меню с картинкой», «покажи мои
  посты», «сделай пост #3 короче».
- Под черновиком четыре кнопки: ✅ Опубликовать сейчас · 🕒 Запланировать («завтра в 10 утра»)
  · ✏️ Изменить («короче, добавь эмодзи») · ❌ Отклонить.
- `/posts`, `/post N` — список постов и карточка одного поста. `/status` — диагностика: LLM,
  dry-run, компания, канал, посты по статусам, задачи планировщика.
- `/channel @канал` — подключить Telegram-канал текущей компании.

**Для администратора** (ID в `ALLOWED_USER_IDS`): `/new_company Название` — добавить компанию,
`/companies` и `/use N` — список и переключение, `/invite` — ссылка-приглашение для клиента.
Как подключить новую компанию — [docs/ONBOARDING_COMPANY.md](docs/ONBOARDING_COMPANY.md).

## Тесты и линтер

```bash
pytest
ruff check .
ruff format --check .
```

Исправить замечания автоматически: `ruff check --fix . && ruff format .`.
Тесты работают без сети и ключей: in-memory SQLite, mock-LLM и фейковая сессия Telegram.

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
