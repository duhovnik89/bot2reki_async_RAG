# Бот поселения Междуречье

## О проекте
Бот для жителей поселения Междуречье — работает в Telegram и ВКонтакте.
Отвечает на вопросы о решениях общего собрания с помощью RAG-поиска (LlamaIndex) и LLM через OpenRouter.

## Стек
- Python + aiogram 3.x (Telegram)
- Python + vkbottle 4.x (ВКонтакте, daemon thread)
- RAG: llama-index-core + VectorStoreIndex в памяти
- Эмбеддинги через OpenRouter API (OpenAI-совместимый /v1/embeddings, класс OpenAILikeEmbedding + подкласс с префиксами e5 «query: »/«passage: »); модель intfloat/multilingual-e5-large
- Генерация: LLM google/gemini-3.1-flash-lite через класс llama-index OpenRouter (llama-index-llms-openrouter), ответы идут через llm.achat()
- Дисковый кэш векторов data/rules.embeddings.npz (ключ — md5 текста фрагмента; смена модели/сервера сбрасывает кэш)
- В промпт LLM идут только топ-K найденных фрагментов
- openpyxl (чтение Excel → rules.json через bot/sync.py)
- python-dotenv (секреты)
- Локальных нейросетевых библиотек (PyTorch и т.п.) нет

## Архитектура
Один процесс, два бота запускаются параллельно. Telegram — через asyncio.gather, VK — в daemon thread со своим event loop (vkbottle несовместим с внешним event loop).

Поток данных: `rules.xlsx` → sync.py → `rules.json` (записи «лист — дата — текст») → RulesEngine при старте режет записи на фрагменты, берёт их векторы из дискового кэша (новые — через OpenRouter API) и строит VectorStoreIndex в памяти → на каждый вопрос векторный поиск топ-12 фрагментов → только они + вопрос уходят в LLM.

Уточняющие вопросы: перед векторным поиском вопрос дополняется последними репликами жителя (`_search_query`); история переписки по-прежнему лежит в бакете HISTORY_S3_BUCKET и идёт модели между системным промптом и вопросом.

Дубли (хронология дублирует тематические листы) выбрасываются после поиска; LongContextReorder расставляет лучшие фрагменты по краям контекста.

Эмбеддер и индекс общие для всех RulesEngine процесса (кэш `_index_cache` по версии файла): TG- и VK-движки не дублируют друг друга. Отказ эмбеддинг-API при сборке индекса не роняет бот и не «запоминается» — сборка повторится на следующем вопросе.

Файл правил: без бакета — `data/rules.json`; с бакетом — RULES_S3_BUCKET (source.py, ETag-проверка раз в RULES_REFRESH_SECONDS, файл качается в /tmp). Пересобрать rules.json: `python -m bot.sync`.

Модель: LLM_MODEL (по умолчанию google/gemini-3.1-flash-lite), temperature 0.1, max_tokens 1024.

Serverless: с API-эмбеддерами пакет снова лёгкий (без PyTorch) — деплой в Yandex Cloud Functions возможен; нюанс — кэш векторов живёт в /tmp только в рамках тёплого инстанса, холодный старт делает запросы к OpenRouter (при полном кэше это доля секунды... нет: на холодном инстансе /tmp пуст, поэтому первая сборка = запросы к API). См. docs/DEPLOY_YANDEX_SERVERLESS.md.

Тесты: `.venv/Scripts/python -m pytest -q tests` (LLM, эмбеддинги и облако подменены, сети нет).

## Правила для Claude
- Не трогай .env (там секреты)
- Секреты храни в .env, читай через python-dotenv
- Логику RAG держи в bot/search.py (класс RulesEngine)
- Telegram-хендлеры — в bot/handlers.py (не telegram_bot.py)
- VK-хендлеры — в bot/vk_bot.py
- VK-бот запускается через threading.Thread + bot.run_forever() — не через asyncio.gather напрямую
- В тестах RulesEngine передавай embed_model (MockEmbedding из test_dialog.py) и cache_path во временный каталог — чтобы не звать API и не трогать общий кэш
- BaseEmbedding — pydantic-модель: свои атрибуты только через PrivateAttr
