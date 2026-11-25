"""Точка входа Yandex Cloud Function. Точка входа в настройках: `serverless_handler.handler`.

Один обработчик на три входа:
  1. Async само-вызов воркера — у события есть ключ `__job__` (нет `httpMethod`).
     Адресат задания (`target`) говорит, чей воркер его выполняет: `tg` или `vk`.
  2. ВКонтакте Callback API — в теле запроса есть `type`.
  3. Telegram webhook — в теле запроса есть `update_id`.
"""
import base64
import hmac
import json
import logging
import sys

# force=True обязателен. Runtime Яндекса настраивает корневой логгер до нашего
# импорта, поэтому обычный basicConfig оказывается no-op: за все деплои в
# лог-группу не попало ни одной строки приложения, и диагностика шла вслепую.
# force сносит чужие обработчики и ставит наш, в stdout — его runtime забирает
# в логи гарантированно.
logging.basicConfig(
    level=logging.INFO,
    force=True,
    stream=sys.stdout,
    format="%(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Создаётся один раз на холодном старте и переживает тёплые вызовы —
# RulesEngine (индекс фрагментов, эмбеддер, LLM) собирается только один раз.
from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession

from bot.async_jobs import is_job_event, verify
from bot.diag import run_diagnostics
from bot.handlers import process_tg_job, router
from bot.vk_callback import handle_vk_event, process_vk_job
from config.settings import JOB_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_PROXY

# Свой таймаут на вызовы Telegram Bot API. Дефолт aiogram — 60 секунд, ровно
# столько же, сколько таймаут функции: один зависший вызов к api.telegram.org
# съедает весь бюджет и функция умирает с `499 Request cancelled`, не оставив в
# логах ни строчки. 15 секунд с запасом хватает на sendMessage.
_tg_bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    session=AiohttpSession(timeout=15, proxy=TELEGRAM_PROXY or None),
)
if TELEGRAM_PROXY:
    logger.info("Вызовы Telegram Bot API идут через прокси")
_tg_dp = Dispatcher()
_tg_dp.include_router(router)


def _read_body(event) -> str:
    body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


async def handler(event, context):
    # event от async само-вызова приходит как уже распарсенный объект, но на всякий
    # случай поддерживаем и строку.
    if isinstance(event, (str, bytes)):
        try:
            event = json.loads(event)
        except (json.JSONDecodeError, TypeError):
            event = {}
    if not isinstance(event, dict):
        event = {}

    # Ключи события — по ним видно, в какую ветку пойдёт обработчик и в каком
    # виде приходит задание от асинхронного вызова. Значений не пишем: там токены.
    logger.info("Событие: ключи=%s", sorted(event.keys()))

    # 0. Самодиагностика сети. Запускается вручную со вкладки «Тестирование»
    # телом {"__diag__": "<VK_SECRET>"}. Функция публична, поэтому под секретом.
    if event.get("__diag__") is not None:
        if (
            event.get("httpMethod") is not None
            or not JOB_SECRET
            or not hmac.compare_digest(
                str(event["__diag__"]).encode("utf-8"), JOB_SECRET.encode("utf-8")
            )
        ):
            logger.error("Самодиагностика отклонена: неверный секрет")
            return {"statusCode": 403, "body": "forbidden"}
        report = await run_diagnostics()
        logger.info("Самодиагностика: отказали %s", report["отказавшие_ручки"])
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(report, ensure_ascii=False, indent=2),
        }

    # 1. Async-задание воркера (вне окна ожидания мессенджера).
    # Функция публична → ветку надо аутентифицировать: HTTP-события от шлюза несут
    # httpMethod (их сюда не пускаем), а тело задания подписано HMAC на JOB_SECRET.
    if is_job_event(event):
        if event.get("httpMethod") is not None or not verify(
            event.get("job"), event.get("sig", "")
        ):
            logger.error(
                "Задание отклонено: подпись не сошлась или пришло через шлюз"
            )
            return {"statusCode": 403, "body": "forbidden"}

        job = event["job"]
        logger.info(
            "Беру задание в работу: target=%s kind=%s",
            job.get("target"),
            job.get("kind"),
        )
        if job.get("target") == "tg":
            await process_tg_job(job, _tg_bot)
        else:
            await process_vk_job(job)
        return {"statusCode": 200, "body": "done"}

    if event.get("httpMethod") != "POST":
        logger.warning("Событие не распознано ни как задание, ни как POST от шлюза")
        return {"statusCode": 405, "body": "method not allowed"}

    raw = _read_body(event)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "bad json"}

    # 2. ВКонтакте Callback API
    if "type" in data:
        return await handle_vk_event(data)

    # 3. Telegram webhook
    if "update_id" in data:
        update = types.Update.model_validate(data, context={"bot": _tg_bot})
        try:
            await _tg_dp.feed_update(_tg_bot, update)
        except Exception:  # noqa: BLE001
            # Без этого падение уходит наружу как `502 Error during function
            # invocation` — без трассировки и без намёка на причину.
            logger.exception("Обработка апдейта %s упала", data.get("update_id"))
            return {"statusCode": 500, "body": "handler error"}
        return {"statusCode": 200, "body": "ok"}

    logger.warning("Тело запроса не распознано: ключи=%s", sorted(data.keys()))
    return {"statusCode": 200, "body": "ignored"}
