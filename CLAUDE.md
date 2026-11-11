# CLAUDE.md — правила проекта «Автономный AI-маркетолог»

Этот файл читают Claude Code у обоих участников команды. Он главный источник согласованности.

## Что за проект

Прототип автономного AI-маркетолога (хакатон, 3 дня, 2 разработчика). Клиент ставит задачи
обычным языком в Telegram-боте. AI-агент (LLM с инструментами) изучает бизнес, готовит посты
и картинки. Публикация в Telegram-канал идёт **только после согласования** кнопкой.
VK и MAX пока заглушки.

Архитектура и поток данных описаны в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), бэклог по
потокам A/B — в [docs/TASKS.md](docs/TASKS.md), принятые решения — в
[docs/DECISIONS.md](docs/DECISIONS.md).

## Где что лежит (правила размещения кода)

| Что | Куда |
|---|---|
| Бизнес-логика | `app/services/` — хендлеры и инструменты вызывают только этот слой |
| Хендлеры бота | `app/bot/handlers/` — **тонкие**: принять ввод → вызвать сервис → показать результат |
| Тексты бота | только `app/bot/texts.py` (HTML-разметка, пользовательский/LLM-текст — через `html.escape`) |
| Клавиатуры и CallbackData | только `app/bot/keyboards.py` |
| FSM-состояния | только `app/bot/states.py` |
| Инструменты агента | только `app/agent/tools/*.py`, декоратор `@tool`; новый файл — добавить импорт в `app/agent/tools/__init__.py` |
| Промпты | только `app/agent/prompts/*.md`, подстановки `{{var}}`, загрузка `render_prompt(name, ...)` |
| Доступ к БД | `app/db/repositories/` — по файлу на сущность; без бизнес-логики |
| Новый LLM-провайдер | `app/llm/<name>_provider.py` + ветка в `app/llm/factory.py` |
| Новый генератор картинок | `app/images/<name>_provider.py` + ветка в `app/images/factory.py` |
| Новый публикатор | `app/publishers/<name>.py` + регистрация в `app/container.py` |
| Фоновые задачи | `app/scheduler/jobs.py` (логика с явными зависимостями) + `setup.py` (расписание) |
| Сборка зависимостей | `app/container.py` |

## Что НЕЛЬЗЯ менять без согласования с напарником

Если задача требует таких изменений — **остановись и прямо скажи пользователю**: «это меняет
общий интерфейс X, согласуй с напарником», предложи вариант и жди решения.

- интерфейсы в `*/base.py` (`LLMProvider`, `ImageProvider`, `Publisher`) и `app/services/notifier.py`;
- модели БД `app/db/models.py`: новые колонки и таблицы — тоже по согласованию, потому что у
  обоих разработчиков одна схема, а миграций пока нет;
- машину состояний поста (`ALLOWED_TRANSITIONS` в `app/services/posts.py`);
- правило «публикация только после согласования»: у агента **нет** инструмента публикации, в
  `approved`/`scheduled` пост переводит только `services/approval.py`, а `services/publishing.py`
  проверяет `ensure_publishable()`. Тесты в `tests/test_post_state_machine.py` это сторожат;
- формат user-сообщений в `services/posts.py`: по нему работает `llm/mock_provider.py`.

## Код и стиль

- Python 3.11+, весь код асинхронный. aiogram 3, SQLAlchemy 2.0 async, pydantic-settings, APScheduler 3.
- Код, имена и коммиты пишем на английском. Комментарии, docstring-и, документацию и тексты бота — на русском.
- Даты храним в БД как **naive UTC** (`app/utils/time.py::utcnow`), пользователю показываем в `TIMEZONE`.
- Сессия БД живёт одно обновление Telegram (`DbSessionMiddleware`): коммит после хендлера.
  Сервисы делают `flush`, а `commit` — только перед внешним действием (отправка черновика,
  публикация).
- Связи ORM в async не грузятся лениво. Данные получай через репозитории.
- Секреты храним только в `.env`. Новая настройка добавляется в `app/config.py` и в
  `.env.example` вместе с русским комментарием.

## Команды

```bash
# активировать venv: Windows — .venv\Scripts\activate ; macOS/Linux — source .venv/bin/activate
python -m app.main --check   # проверить конфиг/БД/агента/планировщик без Telegram
python -m app.main           # запустить бота
pytest                       # тесты (без сети и ключей)
ruff check . && ruff format --check .   # линтер и формат (исправить: ruff check --fix . && ruff format .)
```

Перед коммитом обязательно должны проходить `ruff check .`, `ruff format --check .`, `pytest`
и `python -m app.main --check`. Для разработки — `LLM_PROVIDER=mock`, `IMAGE_PROVIDER=mock`,
`PUBLISH_DRY_RUN=true`.

## Git-правила

- `main` всегда должна запускаться. Прямо в `main` не коммитим.
- Каждый работает в своей ветке `feature/<коротко>` (например `feature/vk-publisher`).
- Часто подтягиваем `main`: `git pull --rebase origin main`.
- Сливаем через Pull Request, мелкими порциями (лучше 3 маленьких PR, чем 1 большой).
- Коммиты оформляем по Conventional Commits: `feat:`, `fix:`, `docs:`, `chore:`, `test:`, `refactor:`.
- Конфликт в общем файле (`models.py`, `container.py`, `texts.py`) решаем вместе с напарником.
- Перед пушем проверяем, что в diff нет `.env`, токенов и `data/`.
