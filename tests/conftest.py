import os
import sys
from pathlib import Path

# Тесты не ходят ни в OpenRouter, ни в облако: ПУСТОЙ ключ (движок при импорте
# handlers честно останется без индекса и без LLM, но сетевых вызовов не будет)
# и пустые бакеты. load_dotenv() не перетирает уже заданные переменные,
# поэтому .env их не вернёт.
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["HISTORY_S3_BUCKET"] = ""
os.environ["RULES_S3_BUCKET"] = ""
os.environ["FUNCTION_ID"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
