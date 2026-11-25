"""Асинхронный само-вызов функции — общий механизм для Telegram и ВК.

Зачем. И Telegram, и ВК ждут ответ на webhook считанные секунды, а ответ LLM
занимает десятки секунд — окно ожидания мессенджера такое не выдерживает.

Как. Медленная работа уходит в отдельный асинхронный вызов этой же функции
(`?integration=async`). Мессенджер получает ответ мгновенно, воркер считает
ответ вне окна ожидания.

Безопасность. Функция публичная — её можно дёрнуть напрямую в обход API
Gateway. Поэтому задание подписывается HMAC-SHA256 на server-only секрете
(`JOB_SECRET`, по умолчанию равен `VK_SECRET`), а обработчик проверяет подпись
и отклоняет всё, что пришло с `httpMethod`.
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Awaitable, Callable

import aiohttp

from config.settings import FUNCTION_ID, JOB_SECRET

logger = logging.getLogger(__name__)

# Ключ конверта само-вызова. Legacy-ключ понимается на приёме, чтобы задание,
# улетевшее до деплоя новой версии, не потерялось.
ENVELOPE_KEY = "__job__"
_LEGACY_ENVELOPE_KEY = "__vk_job__"

_METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)

# Жёсткие таймауты на обе исходящие ручки. Без них зависший metadata-сервис или
# недоступный functions.yandexcloud.net держат синхронную ветку до самого
# таймаута функции — мессенджер рвёт соединение (`499 Request cancelled`), и в
# логах при этом нет ни одной строки об ошибке: код всё ещё ждёт ответа.
# Вместе оба лимита укладываются в окно ожидания мессенджера.
_TOKEN_TIMEOUT = aiohttp.ClientTimeout(total=3)
_INVOKE_TIMEOUT = aiohttp.ClientTimeout(total=7)

# IAM-токен живёт часами и переживает тёплые вызовы — незачем ходить в
# metadata-сервис на каждое задание.
_token_cache: dict = {"token": "", "expires_at": 0.0}


def sign(job: dict) -> str:
    canonical = json.dumps(job, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(JOB_SECRET.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def verify(job, sig: str) -> bool:
    if not JOB_SECRET or not isinstance(job, dict) or not sig:
        return False
    return hmac.compare_digest(sig, sign(job))


def is_job_event(event) -> bool:
    """Событие пришло от само-вызова, а не от API Gateway."""
    return isinstance(event, dict) and bool(
        event.get(ENVELOPE_KEY) or event.get(_LEGACY_ENVELOPE_KEY)
    )


async def _iam_token() -> str:
    now = time.monotonic()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    async with aiohttp.ClientSession(timeout=_TOKEN_TIMEOUT) as session:
        async with session.get(
            _METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}
        ) as resp:
            # Metadata-сервис Яндекса отдаёт JSON с типом text/plain, и
            # resp.json() на нём падает с ContentTypeError. Замер на проде
            # 25.09.2026: HTTP 200 за 3 мс — и отказ на разборе. Из-за этого
            # async-разрыв не включался ни разу, всё уходило в откат на инлайн.
            raw = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"metadata вернул HTTP {resp.status}: {raw[:200]}")
            data = json.loads(raw)

    _token_cache["token"] = data["access_token"]
    # expires_in обычно 12 часов; берём запас в 5 минут
    _token_cache["expires_at"] = now + max(int(data.get("expires_in", 600)) - 300, 60)
    return _token_cache["token"]


async def enqueue(job: dict, fallback: Callable[[], Awaitable[None]]) -> None:
    """Отдать задание в асинхронный вызов функции.

    Любой отказ означает, что житель останется без ответа вообще, поэтому во
    всех ветках отказа задание выполняется инлайн: медленно, с риском словить
    ретрай мессенджера, но с результатом.
    """
    if not FUNCTION_ID or not JOB_SECRET:
        logger.warning(
            "FUNCTION_ID/JOB_SECRET не заданы — выполняю задание инлайн"
        )
        await fallback()
        return

    url = f"https://functions.yandexcloud.net/{FUNCTION_ID}?integration=async"
    payload = json.dumps({ENVELOPE_KEY: True, "job": job, "sig": sign(job)})
    headers = {"Content-Type": "application/json"}
    logger.info(
        "Ставлю задание в async-очередь: target=%s kind=%s",
        job.get("target"),
        job.get("kind"),
    )

    try:
        headers["Authorization"] = f"Bearer {await _iam_token()}"
        async with aiohttp.ClientSession(timeout=_INVOKE_TIMEOUT) as session:
            async with session.post(url, data=payload, headers=headers) as resp:
                status = resp.status
                body = "" if status == 202 else await resp.text()
        if status == 202:
            logger.info("Задание принято асинхронным вызовом (202)")
            return
        logger.error(
            "async invoke вернул %s: %s — выполняю задание инлайн", status, body
        )
    except Exception as e:  # noqa: BLE001
        logger.error(
            "async invoke упал (%s: %s) — выполняю задание инлайн",
            type(e).__name__,
            e,
        )

    await fallback()
