"""IAM-токен сервисного аккаунта функции из metadata-сервиса Yandex Cloud.

Общий для модулей, которые ходят в Object Storage синхронно (db_source, history).
Токен живёт часами и переживает тёплые вызовы — кэшируем с запасом в 5 минут.
"""
import json
import threading
import time
import urllib.request

_METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
_TIMEOUT = 3

_lock = threading.Lock()
_cache = {"token": "", "expires": 0.0}


def token() -> str:
    with _lock:
        now = time.monotonic()
        if _cache["token"] and now < _cache["expires"]:
            return _cache["token"]
        req = urllib.request.Request(_METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            # metadata-сервис отдаёт JSON с типом text/plain — разбираем текст сами
            data = json.loads(resp.read().decode("utf-8"))
        _cache["token"] = data["access_token"]
        _cache["expires"] = now + max(int(data.get("expires_in", 600)) - 300, 60)
        return _cache["token"]
