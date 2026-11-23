"""Откуда бот берёт rules.db: из бакета Object Storage или из пакета функции.

Зачем. Раньше новая база попадала к боту только через пересборку пакета и новую
версию функции — это делает коллега с доступом к облаку, и каждое обновление
решений ждало его. Теперь база лежит в бакете: залил файл — бот подхватил.

Как.
  - DB_S3_BUCKET пуст (локальный запуск, старая схема) — работаем с DB_FILE_PATH,
    всё как раньше.
  - DB_S3_BUCKET задан — на холодном старте качаем объект в /tmp (единственное
    место, куда функция может писать), дальше не чаще раза в DB_REFRESH_SECONDS
    спрашиваем бакет «не изменился ли файл» (If-None-Match по ETag, ответ 304 —
    без скачивания).
  - Любой отказ бакета не роняет бота: остаётся последняя скачанная база, а если
    скачать не удалось ни разу — база из пакета функции.

Доступ — IAM-токеном сервисного аккаунта функции из metadata-сервиса
(заголовок X-YaCloud-SubjectToken). Аккаунту нужна роль storage.viewer на бакет.
Модуль синхронный: вызывается при импорте и из asyncio.to_thread.
"""
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from bot import yc_iam
from config.settings import DB_FILE_PATH, DB_REFRESH_SECONDS, DB_S3_BUCKET, DB_S3_KEY

logger = logging.getLogger(__name__)

_STORAGE_URL = "https://storage.yandexcloud.net"
_LOCAL_COPY = Path("/tmp/rules.db") if os.name != "nt" else Path(os.getenv("TEMP", ".")) / "rules_s3.db"
_TIMEOUT = 5

_lock = threading.Lock()
_state = {
    "path": DB_FILE_PATH,  # что сейчас читать
    "version": "package",  # ETag скачанного объекта или «package»
    "checked_at": 0.0,
}


def _fetch() -> None:
    """Скачать объект, если он изменился. Исключения — наружу, их ловит current()."""
    url = f"{_STORAGE_URL}/{DB_S3_BUCKET}/{urllib.parse.quote(DB_S3_KEY)}"
    headers = {"X-YaCloud-SubjectToken": yc_iam.token()}
    if _state["version"] != "package":
        headers["If-None-Match"] = _state["version"]
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read()
            etag = resp.headers.get("ETag", "") or f"len-{len(body)}"
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return
        raise RuntimeError(f"бакет вернул HTTP {e.code}: {e.read()[:200]!r}") from None

    # SQLite-файл начинается с этой сигнатуры: не подменяем рабочую базу
    # страницей ошибки или обрезком.
    if not body.startswith(b"SQLite format 3\x00"):
        raise RuntimeError(f"объект {DB_S3_KEY} не похож на SQLite ({len(body)} байт)")
    tmp = _LOCAL_COPY.with_suffix(".part")
    tmp.write_bytes(body)
    os.replace(tmp, _LOCAL_COPY)
    _state["path"], _state["version"] = str(_LOCAL_COPY), etag
    logger.info("База из бакета: %s/%s, %d байт, версия %s", DB_S3_BUCKET, DB_S3_KEY, len(body), etag)


def current() -> tuple[str, str]:
    """(путь к базе, версия). Сходит в бакет, если пора проверить обновление."""
    if not DB_S3_BUCKET:
        return DB_FILE_PATH, "package"
    with _lock:
        now = time.monotonic()
        if _state["checked_at"] and now - _state["checked_at"] < DB_REFRESH_SECONDS:
            return _state["path"], _state["version"]
        _state["checked_at"] = now
        try:
            _fetch()
        except Exception as e:  # noqa: BLE001 — бот должен жить без бакета
            logger.error(
                "Не удалось обновить базу из бакета (%s); работаю с %s", e, _state["version"]
            )
        return _state["path"], _state["version"]
