# Бот поселения Междуречье

## О проекте
Бот для жителей поселения Междуречье — работает в Telegram и ВКонтакте.
Отвечает на вопросы о решениях общего собрания по Excel-файлу с помощью DeepSeek AI.

## Стек
- Python + aiogram 3.x (Telegram)
- Python + vkbottle 4.x (ВКонтакте, daemon thread)
- DeepSeek API через openai-клиент (весь архив решений — в system prompt)
- SQLite (хранение решений через bot/sync.py)
- openpyxl (чтение Excel → SQLite через bot/sync.py)
- python-dotenv (секреты)

## Архитектура
Один процесс, два бота запускаются параллельно. Telegram — через asyncio.gather, VK — в daemon thread со своим event loop (vkbottle несовместим с внешним event loop).

```
bot-mezhdureche/
├── main.py                  # точка входа: asyncio.gather(telegram, vk)
├── requirements.txt
├── .env.example
├── config/
│   └── settings.py          # load_dotenv() + все константы
├── bot/
│   ├── handlers.py          # Telegram-хендлеры (Router), /start /help /about
│   ├── vk_bot.py            # VK-хендлеры (BotLabeler) + run_vk_bot()
│   ├── search.py            # RulesEngine: грузит весь архив в память + DeepSeek API
│   ├── history.py           # история переписки жителя (бакет HISTORY_S3_BUCKET)
│   ├── db_source.py         # rules.db из бакета DB_S3_BUCKET
│   ├── yc_iam.py            # IAM-токен функции для Object Storage
│   └── sync.py              # openpyxl → SQLite FTS5 (запускать отдельно при обновлении Excel)
└── data/
    ├── rules.xlsx           # файл правил (не в git)
    └── rules.db             # SQLite база (не в git, генерируется sync.py)
```

## Поток данных
Excel → sync.py → rules.db (SQLite) → RulesEngine загружает весь архив при старте → DeepSeek API получает полный архив в system prompt → ответ жителю.
На serverless rules.db берётся из бакета Object Storage (bot/db_source.py, переменная DB_S3_BUCKET), с проверкой обновлений раз в 5 минут; rules.db из пакета — запасная. Внесение протоколов — скилл `протокол-в-базу`.
Если ответа нет → маркер [НУЖЕН_АДМИНИСТРАТОР] → текст с контактом @duhovnik_89.

История переписки (bot/history.py): отдельный бакет HISTORY_S3_BUCKET, объект на разговор `history/<tg|vk>/<чат>/<житель>.json`. Модели — последние 10 сообщений за час, по 1000 символов, между системным промптом и вопросом (база решений остаётся кэшируемым префиксом). Хранение 7 дней (правило жизненного цикла бакета + чистка в коде). /start и «старт» в ВК стирают историю. Хранилище недоступно → ответ без истории + ERROR в логе. Неудачные ответы (ошибка DeepSeek) в историю не пишутся.

Модель: DEEPSEEK_MODEL (по умолчанию deepseek-flash) с `thinking: disabled` — у flash рассуждения включены по умолчанию.

Тесты: `.venv/Scripts/python -m pytest -q tests` (DeepSeek и облако подменены).

## Правила для Claude
- Не трогай .env (там секреты)
- Секреты храни в .env, читай через python-dotenv
- Логику поиска держи в bot/search.py (класс RulesEngine)
- Telegram-хендлеры — в bot/handlers.py (не telegram_bot.py)
- VK-хендлеры — в bot/vk_bot.py
- VK-бот запускается через threading.Thread + bot.run_forever() — не через asyncio.gather напрямую
