"""Откуда бот берёт rules.json: из бакета Object Storage или из пакета функции.

Зачем. Новые решения попадают к боту без пересборки пакета: залил файл в бакет —
бот подхватил. Механизм возит rules.json, который готовит bot/sync.py из Excel.

Как.
  - RULES_S3_BUCKET пуст (локальный запуск, старая схема) — работаем с
    RULES_JSON_PATH, всё как раньше.
  - RULES_S3_BUCKET задан — на холодном старте качаем объект в /tmp (единственное
    место, куда функция может писать), дальше не чаще раза в RULES_REFRESH_SECONDS
    спрашиваем бакет «не изменился ли файл» (If-None-Match по ETag, ответ 304 —
    без скачивания).
  - Любой отказ бакета не роняет бота: остаётся последняя скачанная версия, а если
    скачать не удалось ни разу — файл из пакета.

Доступ — IAM-токеном сервисного аккаунта функции из metadata-сервиса
(заголовок X-YaCloud-SubjectToken). Аккаунту нужна роль storage.viewer на бакет.
Модуль синхронный: вызывается при импорте и из asyncio.to_thread.
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from bot import yc_iam
from config.settings import (
    RULES_JSON_PATH,
    RULES_REFRESH_SECONDS,
    RULES_S3_BUCKET,
    RULES_S3_KEY,
)

logger = logging.getLogger(__name__)

_STORAGE_URL = "https://storage.yandexcloud.net"
_LOCAL_COPY = (
    Path("/tmp/rules.json") if os.name != "nt" else Path(os.getenv("TEMP", ".")) / "rules_s3.json"
)
_TIMEOUT = 5

_lock = threading.Lock()
_state = {
    "path": RULES_JSON_PATH,  # что сейчас читать
    "version": "package",  # ETag скачанного объекта или «package»
    "checked_at": 0.0,
}


def _looks_like_rules(body: bytes) -> bool:
    """Не подменяем рабочий архив страницей ошибки или обрезком."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and isinstance(data.get("records"), list)


def _fetch() -> None:
    """Скачать объект, если он изменился. Исключения — наружу, их ловит current()."""
    url = f"{_STORAGE_URL}/{RULES_S3_BUCKET}/{urllib.parse.quote(RULES_S3_KEY)}"
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

    if not _looks_like_rules(body):
        raise RuntimeError(f"объект {RULES_S3_KEY} не похож на rules.json ({len(body)} байт)")
    tmp = _LOCAL_COPY.with_suffix(".part")
    tmp.write_bytes(body)
    os.replace(tmp, _LOCAL_COPY)
    _state["path"], _state["version"] = str(_LOCAL_COPY), etag
    logger.info(
        "rules.json из бакета: %s/%s, %d байт, версия %s",
        RULES_S3_BUCKET, RULES_S3_KEY, len(body), etag,
    )


def current() -> tuple[str, str]:
    """(путь к файлу, версия). Сходит в бакет, если пора проверить обновление."""
    if not RULES_S3_BUCKET:
        return RULES_JSON_PATH, "package"
    with _lock:
        now = time.monotonic()
        if _state["checked_at"] and now - _state["checked_at"] < RULES_REFRESH_SECONDS:
            return _state["path"], _state["version"]
        _state["checked_at"] = now
        try:
            _fetch()
        except Exception as e:  # noqa: BLE001 — бот должен жить без бакета
            logger.error(
                "Не удалось обновить rules.json из бакета (%s); работаю с %s", e, _state["version"]
            )
        return _state["path"], _state["version"]
