"""Самодиагностика сети из функции. Запускается вручную через вкладку
«Тестирование» на странице функции.

Зачем. Логи 24.09.2026 показали запуск на 45,5 секунды, завершившийся без
ошибок, — и без ответа жителю. ВК с той же функции при этом отвечает быстро.
Догадки кончились: нужно измерить каждую внешнюю ручку по отдельности и
увидеть, какая из них не отвечает.

Как запустить. Функция → «Тестирование» → в теле запроса:

    {"__diag__": "<значение VK_SECRET>"}

Ответ — таблица по каждой ручке: сколько миллисекунд, что вернула, какая
ошибка. Секрет нужен, потому что функция публична.
"""
import asyncio
import json
import socket
import time

import aiohttp

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_PROXY

_TIMEOUT = aiohttp.ClientTimeout(total=10)

_METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)

# Токен бота стоит прямо в пути getMe, а в адресе прокси бывают логин и пароль.
# aiohttp охотно кладёт полный URL в текст исключения, а отчёт уходит и в логи,
# и в тело ответа — поэтому вычищаем секреты из любой строки отчёта. Длинные
# заменяем первыми, чтобы частичное совпадение не съело целое.
_SECRETS = sorted(
    {
        v
        for v in (
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_BOT_TOKEN.split(":", 1)[-1],
            TELEGRAM_PROXY,
        )
        if v
    },
    key=len,
    reverse=True,
)


def _redact(text: str) -> str:
    for secret in _SECRETS:
        text = text.replace(secret, "***")
    return text


async def _probe(name: str, coro) -> dict:
    started = time.monotonic()
    try:
        detail = await coro
        return {
            "ручка": name,
            "итог": "ОК",
            "мс": round((time.monotonic() - started) * 1000),
            "детали": _redact(str(detail)),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ручка": name,
            "итог": "ОТКАЗ",
            "мс": round((time.monotonic() - started) * 1000),
            "ошибка": _redact(f"{type(e).__name__}: {e}"),
        }


async def _dns(host: str) -> str:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return ", ".join(sorted({i[4][0] for i in infos}))


async def _tcp(host: str, port: int = 443) -> str:
    # Без своего лимита проверка ждёт таймаута ОС: на проде 25.09.2026 отказ
    # api.telegram.org занял 130 секунд и растянул весь прогон.
    _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
    writer.close()
    await writer.wait_closed()
    return f"соединение с {host}:{port} установлено"


async def _get(url: str, *, proxy: str = "", expect_json: bool = False) -> str:
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.get(url, proxy=proxy or None) as resp:
            if expect_json:
                data = await resp.json()
                result = data.get("result") or {}
                return f"HTTP {resp.status}, бот @{result.get('username', '?')}"
            await resp.read()
            return f"HTTP {resp.status}"


async def _metadata_token() -> str:
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.get(
            _METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}
        ) as resp:
            # Ответ приходит как text/plain — разбираем сами, без resp.json().
            raw = await resp.text()
            token = ""
            try:
                token = json.loads(raw).get("access_token", "")
            except ValueError:
                pass
            return f"HTTP {resp.status}, токен получен: {bool(token)}"


async def run_diagnostics() -> dict:
    """Проверки идут по возрастанию уровня: имя → TCP → HTTPS.

    Так видно, на каком именно слое рвётся: не резолвится имя, не встаёт
    соединение или не проходит уже сам запрос.
    """
    probes = [
        _probe("DNS api.telegram.org", _dns("api.telegram.org")),
        _probe("TCP api.telegram.org:443", _tcp("api.telegram.org")),
        _probe(
            "HTTPS api.telegram.org (getMe)",
            _get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe",
                proxy=TELEGRAM_PROXY,
                expect_json=True,
            ),
        ),
        _probe("DNS api.vk.com", _dns("api.vk.com")),
        _probe("TCP api.vk.com:443", _tcp("api.vk.com")),
        _probe("HTTPS api.vk.com", _get("https://api.vk.com/method/utils.getServerTime")),
        _probe("Metadata-сервис (IAM-токен)", _metadata_token()),
        _probe("DNS functions.yandexcloud.net", _dns("functions.yandexcloud.net")),
        _probe("TCP functions.yandexcloud.net:443", _tcp("functions.yandexcloud.net")),
    ]
    results = await asyncio.gather(*probes)
    отказы = [r["ручка"] for r in results if r["итог"] == "ОТКАЗ"]
    return {
        "прокси_для_telegram": bool(TELEGRAM_PROXY),
        "отказавшие_ручки": отказы or "нет",
        "проверки": results,
    }
