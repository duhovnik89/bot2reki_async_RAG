import os
from pathlib import Path
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent.parent

load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
VK_TOKEN: str = os.getenv("VK_TOKEN", "")
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# deepseek-chat снят из списка моделей 24.07.2026 (сервер пока принимает его как
# старое имя deepseek-flash). Текущее имя — deepseek-flash.
DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
RULES_FILE_PATH: str = os.getenv("RULES_FILE_PATH", str(_PROJECT_ROOT / "data" / "rules.xlsx"))
DB_FILE_PATH: str = os.getenv("DB_FILE_PATH", str(_PROJECT_ROOT / "data" / "rules.db"))
ADMIN_CONTACT: str = os.getenv("ADMIN_CONTACT", "@duhovnik_89")

# --- база решений в Object Storage (см. bot/db_source.py) ---
# Бакет пуст — база берётся из DB_FILE_PATH (пакет функции / локальный файл).
DB_S3_BUCKET: str = os.getenv("DB_S3_BUCKET", "")
DB_S3_KEY: str = os.getenv("DB_S3_KEY", "rules.db")
# Как часто тёплая функция спрашивает бакет о новой версии базы.
DB_REFRESH_SECONDS: int = int(os.getenv("DB_REFRESH_SECONDS", "300"))

# --- история переписки (см. bot/history.py) ---
# Отдельный бакет: история не зависит от rules.db, а у функции нет права
# записи туда, где лежит база решений. Пусто — история в памяти процесса
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
# мессенджера и похода в DeepSeek). Общий для Telegram и ВК.
FUNCTION_ID: str = os.getenv("FUNCTION_ID", "")
# Прокси для вызовов Telegram Bot API. Замер 23.09.2026 показал, что
# api.telegram.org из функции периодически не отвечает вовсе (ровно 15 секунд
# ожидания и таймаут). Если провал устойчивый — сюда кладётся адрес прокси,
# например http://user:pass@host:port. Пусто — идём напрямую.
TELEGRAM_PROXY: str = os.getenv("TELEGRAM_PROXY", "")
# Секрет для HMAC-подписи заданий само-вызова. Отдельной переменной на сервере
# не требует: по умолчанию берётся уже заданный VK_SECRET.
JOB_SECRET: str = os.getenv("JOB_SECRET", "") or VK_SECRET
