# OpenAI для AI-маркетолога

Для хакатона текст подключаем через OpenAI API. В репозитории уже есть адаптер
`app/llm/openai_provider.py` на Chat Completions. Он передаёт JSON-схемы инструментов,
разбирает вызовы функций, поддерживает JSON-режим и отдаёт фактическое число токенов
в учёт расходов, когда API его возвращает.

## Настройки

| Компонент | Что выбрать | Текущий статус |
|---|---|---|
| Текст | `LLM_PROVIDER=openai`, `LLM_BASE_URL=https://api.openai.com/v1`, `LLM_MODEL=gpt-4.1-mini`, `LLM_API_KEY` | Адаптер готов. Нужна проверка с ключом и тестовым ботом |
| Изображения для локальной проверки | `IMAGE_PROVIDER=mock` или фото из фотобанка | Работает |
| Генерация изображений | `IMAGE_PROVIDER=openai`, `IMAGE_MODEL=gpt-image-2.5-flare` | Провайдер готов, нужен прогон с ключом |
| Локальная разработка без API-ключа | `LLM_PROVIDER=mock`, `IMAGE_PROVIDER=mock`, `PUBLISH_DRY_RUN=true` | Тесты и проверка запуска работают без сети |

`gpt-4.1-mini` — модель для первого прогона с текущим адаптером. Для другой модели
проверьте поддержку Chat Completions, вызова функций, JSON-режима и параметра
`temperature`, который адаптер отправляет в запросе.

Документация: [быстрый старт](https://developers.openai.com/api/docs/quickstart),
[модель GPT‑4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini),
[генерация изображений](https://developers.openai.com/api/docs/guides/image-generation),
[тарифы API](https://developers.openai.com/api/docs/pricing).

OpenAI Images использует `LLM_API_KEY` или отдельный `IMAGE_API_KEY`, если он задан.
Доступ к Images API зависит от настроек проекта и организации OpenAI.

## ChatGPT и критерий 12

Бот использует ключ проекта API Platform. Для подходящих подписок ChatGPT Pro и Business
OpenAI описывает возможность привязать API-кредиты к организации. Проверьте наличие
этой опции и баланс в своём аккаунте. Даже при активных кредитах бот работает с ключом
проекта. [Условия API-кредитов](https://developers.openai.com/api/docs/guides/chatgpt-api-credits)

Отдельный демонстрационный сценарий: пользователь ставит задачу в ChatGPT, а GPT Action
или MCP-приложение вызывает защищённый HTTPS-интерфейс проекта и отправляет черновик на
согласование в Telegram. Интерфейса и интеграции пока нет. Результат проверки конкретного
аккаунта зафиксируйте в `docs/CHATGPT.md` по задаче A3.

## Расходы и доступность

Для оценки нагрузки используйте [COSTS.md](COSTS.md). До живого прогона выставьте
`LLM_PRICE_INPUT_PER_1M`, `LLM_PRICE_OUTPUT_PER_1M` и, после подключения генерации,
`IMAGE_PRICE` по выбранному тарифу в рублях. Значения в шаблоне исторические.
`/costs` — оценка расходов по компаниям в боте. Фактическое списание видно в API Platform.

Проверяйте доступ к `api.openai.com` и `api.telegram.org` из контейнера на каждой
машине, где запускается бот. Для Telegram предусмотрен `TELEGRAM_PROXY`.
См. [инструкцию запуска](../README.md#локальный-запуск-на-macos-в-docker).
