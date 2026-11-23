import os
import sys
from pathlib import Path

# Тесты не ходят ни в DeepSeek, ни в облако: фиктивный ключ и пустые бакеты.
# load_dotenv() не перетирает уже заданные переменные, поэтому .env их не вернёт.
os.environ["DEEPSEEK_API_KEY"] = "test-key"
os.environ["HISTORY_S3_BUCKET"] = ""
os.environ["DB_S3_BUCKET"] = ""
os.environ["FUNCTION_ID"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
