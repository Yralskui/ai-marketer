# Бэклог

Работа разбита на два потока с минимальным пересечением файлов.
Приоритеты: **P0** — нужно для демо, **P1** — желательно, **P2** — если останется время.

Общие файлы, которые могут понадобиться обоим: `app/db/models.py`, `app/container.py`,
`app/bot/texts.py`, `app/bot/keyboards.py`. Правки в них делаем маленькими отдельными PR и
предупреждаем напарника.

## Уже готово (каркас)

- Бот: `/start`, `/help`, `/onboarding` (текст, файлы, сайт), `/profile`, `/posts`, `/post N`,
  `/status`, `/cancel`, белый список пользователей.
- Агент с инструментами, mock-LLM и OpenAI-совместимый провайдер.
- Черновик с кнопками «опубликовать / запланировать / изменить / отклонить», машина состояний.
- Публикация в Telegram-канал (текст, фото с подписью), dry-run, планировщик раз в минуту.
- Тесты: 60 штук, включая сквозной сценарий бота без сети.

---

## Поток A — «мозги и контент»

Основные файлы: `app/agent/**`, `app/llm/**`, `app/images/**`, `app/ingestion/**`,
`app/services/business_profile.py`, `app/services/content_plan.py`,
`app/services/posts.py` (только генерация, без машины состояний),
`app/bot/handlers/onboarding.py`, новый `app/bot/handlers/plan.py`.

| # | Задача | Файлы | Критерий готовности | Приор. |
|---|---|---|---|---|
| A1 | Подключить реальную LLM и отладить промпты на живой модели: система, пост, правка, профиль | `agent/prompts/*.md`, `.env` | С `LLM_PROVIDER=openai` сценарий «онбординг → пост → правка» даёт качественные тексты, агент выбирает правильные инструменты | P0 |
| A2 | Реальная генерация изображений (OpenAI Images / Kandinsky / YandexART) + отдельный промпт «описание сцены» | `images/<name>_provider.py`, `images/factory.py`, `posts.create_draft`, `agent/prompts/image.md` | «Пост с картинкой» приносит осмысленную картинку; при ошибке генерации пост создаётся без картинки и с предупреждением | P0 |
| A3 | Контент-план на неделю/месяц: генерация через LLM (JSON), показ клиенту, согласование плана целиком кнопками | `services/content_plan.py`, `agent/tools/plan_tools.py`, `agent/prompts/content_plan.md`, `bot/handlers/plan.py`, `db/repositories/content_plan.py` | «Составь контент-план на неделю» → сообщение с 5–7 темами и датами и кнопками «Утвердить / Изменить»; после утверждения план в статусе `approved` | P0 |
| A4 | Автономные черновики по плану: `generate_daily_drafts` создаёт посты по наступившим пунктам и шлёт на согласование | `scheduler/jobs.py::generate_daily_drafts`, `services/content_plan.py::due_items` | При `ENABLE_AUTO_DRAFTS=true` в 9:00 приходит черновик по пункту плана, пост связан с `plan_item_id` | P1 |
| A5 | Профиль из сайта: несколько страниц, sitemap, лучшее выделение текста | `ingestion/website.py` | Ссылка на сайт малого бизнеса даёт осмысленный профиль; тест на `pick_internal_links` | P1 |
| A6 | Суммаризация больших документов по частям (map-reduce) вместо обрезки до 15k символов | `services/business_profile.py` | PDF на 50+ страниц обрабатывается без потери ключевых фактов, укладывается в лимит контекста | P1 |
| A7 | Стиль постов под платформу (TG / VK / MAX): разные промпты и длина | `agent/prompts/write_post_*.md`, `services/posts.py` | Для `platform=vk` пост длиннее и с другой структурой | P2 |
| A8 | Агент уточняет недостающие данные (цены, даты акции), а не выдумывает их | `agent/prompts/system.md` | На «напиши про акцию» без деталей агент задаёт 1 уточняющий вопрос | P2 |
| A9 | Провайдер YandexGPT или GigaChat | `llm/<name>_provider.py`, `llm/factory.py`, `config.py`, `.env.example` | Сценарий работает с `LLM_PROVIDER=yandexgpt` | P2 |

## Поток B — «доставка и метрики»

Основные файлы: `app/publishers/**`, `app/services/publishing.py`,
`app/services/analytics.py`, `app/services/reports.py`, `app/scheduler/**`,
`app/bot/handlers/admin.py`, `app/agent/tools/analytics_tools.py`, деплой (`Dockerfile`,
`docker-compose.yml`, `deploy/`).

| # | Задача | Файлы | Критерий готовности | Приор. |
|---|---|---|---|---|
| B1 | Проверить живую публикацию в канал: права бота, приватный и публичный канал, ссылки, фото + длинный текст | `publishers/telegram.py` | Пост с картинкой и без неё появляется в канале, `/status` показывает «✅ бот — администратор», ссылка открывается | P0 |
| B2 | Надёжность публикации: автоповтор временных ошибок (сеть, 429 `retry_after`) с паузой, лимит попыток, кнопка «Повторить» для `failed` | `services/publishing.py`, `scheduler/jobs.py`, `bot/handlers/approval.py` | Сбой сети при публикации → до 3 повторов → `failed` с понятной ошибкой и кнопкой повтора; тест | P0 |
| B3 | Деплой на сервер: Dockerfile + docker-compose, volume для `data/`, автоперезапуск, инструкция | `Dockerfile`, `docker-compose.yml`, `README.md` | `docker compose up -d` поднимает бота, БД переживает перезапуск | P0 |
| B4 | Публикатор VK: `wall.post`, загрузка фото, ссылка, `check_channel` | `publishers/vk.py`, `config.py`, `.env.example`, `container.py` | Пост с `platform=vk` публикуется в тестовую группу | P1 |
| B5 | Аналитика: сбор метрик (просмотры, реакции) по опубликованным постам, хранение снимков (новая модель `PostMetric` — согласовать) | `services/analytics.py`, `db/models.py`, `scheduler/jobs.py` | `get_analytics` отвечает реальными цифрами за 7 дней | P1 |
| B6 | Еженедельный отчёт: текст от LLM + график картинкой (matplotlib/Pillow), отправка владельцу | `services/reports.py`, `scheduler/jobs.py::weekly_report` | При `ENABLE_WEEKLY_REPORT=true` в понедельник 10:00 приходит отчёт; `make_report` работает по запросу | P1 |
| B7 | Команды администрирования: отмена расписания, перенос времени, `/channel` (сменить канал), удаление черновика | `bot/handlers/admin.py`, `services/approval.py` | Запланированный пост можно отменить и перенести из бота | P1 |
| B8 | Публикатор MAX | `publishers/max.py` | Пост публикуется в канал MAX | P2 |
| B9 | FSM в Redis и Alembic-миграции | `bot/setup.py`, `alembic/` | Состояния переживают рестарт; схема обновляется миграцией | P2 |

## Как брать задачу

1. Перенести задачу в раздел «В работе» ниже (с именем) отдельным коммитом в `main` или
   сообщить напарнику.
2. Создать ветку `feature/<коротко>` и работать небольшими PR.
3. Готово = код + тест (где возможно) + `ruff` + `pytest` + `--check` зелёные, задача
   отмечена ✅ в этом файле.

## В работе

- —
