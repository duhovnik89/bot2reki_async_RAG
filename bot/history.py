"""История переписки жителя с ботом — чтобы понимать уточняющие вопросы.

Зачем. На «можно держать кур?» → «а если их десять?» второй вопрос без первого
непонятен. Модель получает последние сообщения этого разговора вместе с базой
решений.

Где. Serverless-функция ничего не помнит между вызовами, поэтому история лежит
в отдельном бакете Object Storage (HISTORY_S3_BUCKET): один JSON-объект на
разговор, ключ `history/<платформа>/<чат>/<житель>.json`. Telegram и ВК —
разные префиксы; в общем чате у каждого жителя своя история.

Сроки. Правило жизненного цикла бакета удаляет объект через
HISTORY_RETENTION_DAYS после последней записи. Код дополнительно выбрасывает
сообщения старше этого срока при каждой записи.

Отказы. Любая ошибка хранилища пишется в лог (ERROR) и не мешает ответу: бот
отвечает без истории. Модуль синхронный, async-обёртки уводят его в поток.
"""
import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Protocol

from bot import yc_iam
from config.settings import (
    HISTORY_MAX_MESSAGES,
    HISTORY_MESSAGE_CHARS,
    HISTORY_RETENTION_DAYS,
    HISTORY_S3_BUCKET,
    HISTORY_WINDOW_MINUTES,
)

logger = logging.getLogger(__name__)

_STORAGE_URL = "https://storage.yandexcloud.net"
_PREFIX = "history/"
# Короткий таймаут: история — необязательное дополнение к ответу.
_TIMEOUT = 3
# Больше этого в объекте не держим, даже если житель пишет без перерыва.
_STORE_LIMIT = 40


def conversation_key(platform: str, chat_id: int | str, user_id: int | str) -> str:
    """Ключ разговора. И чат, и житель — чтобы в общем чате истории не смешивались."""
    return f"{platform}/{chat_id}/{user_id}"


class Backend(Protocol):
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, body: bytes) -> None: ...
    def delete(self, key: str) -> None: ...


class MemoryBackend:
    """Локальный запуск и тесты: история живёт, пока жив процесс."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, body: bytes) -> None:
        with self._lock:
            self._data[key] = body

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class S3Backend:
    """Object Storage по HTTP с IAM-токеном сервисного аккаунта функции.

    Аккаунту нужна роль storage.editor на бакет истории (чтение, запись, удаление).
    """

    def __init__(
        self,
        bucket: str,
        endpoint: str = _STORAGE_URL,
        token_fn: Callable[[], str] = yc_iam.token,
    ) -> None:
        self._bucket = bucket
        self._endpoint = endpoint.rstrip("/")
        self._token_fn = token_fn

    def _request(self, method: str, key: str, body: bytes | None = None):
        url = f"{self._endpoint}/{self._bucket}/{urllib.parse.quote(_PREFIX + key)}.json"
        headers = {"X-YaCloud-SubjectToken": self._token_fn()}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        return urllib.request.urlopen(req, timeout=_TIMEOUT)

    def get(self, key: str) -> bytes | None:
        try:
            with self._request("GET", key) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise RuntimeError(f"бакет истории вернул HTTP {e.code} на GET") from None

    def put(self, key: str, body: bytes) -> None:
        try:
            with self._request("PUT", key, body):
                pass
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"бакет истории вернул HTTP {e.code} на PUT") from None

    def delete(self, key: str) -> None:
        try:
            with self._request("DELETE", key):
                pass
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return
            raise RuntimeError(f"бакет истории вернул HTTP {e.code} на DELETE") from None


def _trim(text: str) -> str:
    text = text or ""
    if len(text) <= HISTORY_MESSAGE_CHARS:
        return text
    return text[: HISTORY_MESSAGE_CHARS - 1] + "…"


class HistoryStore:
    def __init__(self, backend: Backend, now: Callable[[], float] = time.time) -> None:
        self._backend = backend
        self._now = now

    def _read(self, conv: str) -> list[dict]:
        raw = self._backend.get(conv)
        if not raw:
            return []
        data = json.loads(raw.decode("utf-8"))
        messages = data.get("messages", []) if isinstance(data, dict) else []
        return [
            m for m in messages
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
            and isinstance(m.get("ts"), (int, float))
        ]

    def load(self, conv: str) -> list[dict]:
        """Последние сообщения разговора для модели: [{"role", "content"}].

        Отказ хранилища → пустая история и ERROR в логе.
        """
        try:
            messages = self._read(conv)
        except Exception as e:  # noqa: BLE001 — без истории бот всё равно отвечает
            logger.error("История %s: прочитать не удалось (%s) — отвечаю без истории", conv, e)
            return []
        since = self._now() - HISTORY_WINDOW_MINUTES * 60
        recent = [m for m in messages if m["ts"] >= since][-HISTORY_MAX_MESSAGES:]
        # Диалог для модели начинается с вопроса жителя, а не с середины ответа.
        while recent and recent[0]["role"] != "user":
            recent.pop(0)
        return [{"role": m["role"], "content": m["content"]} for m in recent]

    def append(self, conv: str, question: str, answer: str) -> None:
        """Дописать пару «вопрос — ответ». Отказ → ERROR в логе, ответу не мешает."""
        now = self._now()
        try:
            try:
                messages = self._read(conv)
            except Exception as e:  # noqa: BLE001
                # Прочитать не смогли — пишем только новую пару, чтобы не зависнуть
                # на испорченном объекте навсегда.
                logger.error("История %s: прежняя не прочиталась (%s), начинаю заново", conv, e)
                messages = []
            keep_since = now - HISTORY_RETENTION_DAYS * 86400
            messages = [m for m in messages if m["ts"] >= keep_since]
            messages += [
                {"role": "user", "content": _trim(question), "ts": now},
                {"role": "assistant", "content": _trim(answer), "ts": now},
            ]
            body = json.dumps(
                {"messages": messages[-_STORE_LIMIT:]}, ensure_ascii=False
            ).encode("utf-8")
            self._backend.put(conv, body)
        except Exception as e:  # noqa: BLE001
            logger.error("История %s: сохранить не удалось (%s)", conv, e)

    def clear(self, conv: str) -> bool:
        """Стереть разговор (житель начал заново). True — стёрто."""
        try:
            self._backend.delete(conv)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("История %s: очистить не удалось (%s)", conv, e)
            return False

    async def aload(self, conv: str) -> list[dict]:
        return await asyncio.to_thread(self.load, conv)

    async def aappend(self, conv: str, question: str, answer: str) -> None:
        await asyncio.to_thread(self.append, conv, question, answer)

    async def aclear(self, conv: str) -> bool:
        return await asyncio.to_thread(self.clear, conv)


def default_store() -> HistoryStore:
    if HISTORY_S3_BUCKET:
        logger.info("История переписки: бакет %s", HISTORY_S3_BUCKET)
        return HistoryStore(S3Backend(HISTORY_S3_BUCKET))
    logger.warning("HISTORY_S3_BUCKET не задан — история только в памяти процесса")
    return HistoryStore(MemoryBackend())
