import os
from pathlib import Path
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent.parent

load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
VK_TOKEN: str = os.getenv("VK_TOKEN", "")

# --- OpenRouter: и генерация (LLM), и эмбеддинги RAG ходят в один эндпоинт ---
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL: str = os.getenv("LLM_MODEL", "google/gemini-3.1-flash-lite")
RULES_FILE_PATH: str = os.getenv("RULES_FILE_PATH", str(_PROJECT_ROOT / "data" / "rules.xlsx"))
ADMIN_CONTACT: str = os.getenv("ADMIN_CONTACT", "@duhovnik_89")

# --- архив решений (см. bot/sync.py и bot/source.py) ---
# sync.py готовит rules.json из Excel; бот строит из него индекс в памяти.
RULES_JSON_PATH: str = os.getenv("RULES_JSON_PATH", str(_PROJECT_ROOT / "data" / "rules.json"))
# Бакет пуст — rules.json берётся из RULES_JSON_PATH (пакет функции / локальный файл).
RULES_S3_BUCKET: str = os.getenv("RULES_S3_BUCKET", "")
RULES_S3_KEY: str = os.getenv("RULES_S3_KEY", "rules.json")
# Как часто тёплая функция спрашивает бакет о новой версии файла.
RULES_REFRESH_SECONDS: int = int(os.getenv("RULES_REFRESH_SECONDS", "300"))

# --- RAG (см. bot/search.py) ---
# Эмбеддинги идут через тот же OpenRouter API (/v1/embeddings),
# локального PyTorch больше нет.
EMBED_MODEL: str = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-large")
EMBED_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
# e5-модели обучены с префиксами «query: »/«passage: » — без них поиск хуже.
RAG_EMBED_QUERY_PREFIX: str = os.getenv("RAG_EMBED_QUERY_PREFIX", "query: ")
RAG_EMBED_PASSAGE_PREFIX: str = os.getenv("RAG_EMBED_PASSAGE_PREFIX", "passage: ")
# Записи архива — готовые фрагменты; режутся только длиннее
# RAG_CHUNK_SIZE * 2 символов (размер куска — в токенах).
RAG_CHUNK_SIZE: int = int(os.getenv("RAG_CHUNK_SIZE", "400"))
RAG_CHUNK_OVERLAP: int = int(os.getenv("RAG_CHUNK_OVERLAP", "64"))
# Сколько фрагментов доставать на вопрос.
RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "12"))
# Сколько фрагментов за один запрос к эмбеддинг-API.
RAG_EMBED_BATCH_SIZE: int = int(os.getenv("RAG_EMBED_BATCH_SIZE", "64"))
# Дисковый кэш векторов фрагментов: после первого запуска индекс собирается
# без вызовов API, меняются только новые/изменённые записи.
RAG_EMBEDDINGS_CACHE: str = os.getenv(
    "RAG_EMBEDDINGS_CACHE", str(_PROJECT_ROOT / "data" / "rules.embeddings.npz")
)
# Предохранитель на размер контекста в промпте.
RAG_CONTEXT_MAX_CHARS: int = int(os.getenv("RAG_CONTEXT_MAX_CHARS", "12000"))

# --- история переписки (см. bot/history.py) ---
# Отдельный бакет: история не зависит от архива решений, а у функции нет права
# записи туда, где лежит rules.json. Пусто — история в памяти процесса
# (локальный запуск; на serverless между вызовами она теряется).
HISTORY_S3_BUCKET: str = os.getenv("HISTORY_S3_BUCKET", "")
# Сколько последних сообщений (вопросы + ответы) передаём модели.
HISTORY_MAX_MESSAGES: int = int(os.getenv("HISTORY_MAX_MESSAGES", "10"))
# Учитываем только переписку за это окно — старые темы не мешают новым.
HISTORY_WINDOW_MINUTES: int = int(os.getenv("HISTORY_WINDOW_MINUTES", "60"))
# Каждое сообщение в истории обрезается до стольких символов.
HISTORY_MESSAGE_CHARS: int = int(os.getenv("HISTORY_MESSAGE_CHARS", "1000"))
# Срок хранения. Должен совпадать с правилом жизненного цикла бакета.
HISTORY_RETENTION_DAYS: int = int(os.getenv("HISTORY_RETENTION_DAYS", "7"))

# --- serverless (Yandex Cloud Functions) ---
VK_CONFIRMATION: str = os.getenv("VK_CONFIRMATION", "")
VK_SECRET: str = os.getenv("VK_SECRET", "")
# ID самой функции — нужен для асинхронного само-вызова (разрыв ожидания
# мессенджера и похода в LLM). Общий для Telegram и ВК.
FUNCTION_ID: str = os.getenv("FUNCTION_ID", "")
# Прокси для вызовов Telegram Bot API. Замер 23.09.2026 показал, что
# api.telegram.org из функции периодически не отвечает вовсе (ровно 15 секунд
# ожидания и таймаут). Если провал устойчивый — сюда кладётся адрес прокси,
# например http://user:pass@host:port. Пусто — идём напрямую.
TELEGRAM_PROXY: str = os.getenv("TELEGRAM_PROXY", "")
# Секрет для HMAC-подписи заданий само-вызова. Отдельной переменной на сервере
# не требует: по умолчанию берётся уже заданный VK_SECRET.
JOB_SECRET: str = os.getenv("JOB_SECRET", "") or VK_SECRET
