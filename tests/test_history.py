"""История переписки: изоляция жителей, окно, срок хранения, отказы хранилища."""
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bot.history import HistoryStore, MemoryBackend, S3Backend, conversation_key


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return HistoryStore(MemoryBackend(), now=clock)


def test_follow_up_sees_previous_question(store):
    conv = conversation_key("tg", 100, 100)
    store.append(conv, "Можно держать кур?", "Да, по решению от 06.06.2026.")
    assert store.load(conv) == [
        {"role": "user", "content": "Можно держать кур?"},
        {"role": "assistant", "content": "Да, по решению от 06.06.2026."},
    ]


def test_residents_do_not_see_each_other(store):
    alice = conversation_key("tg", 1, 1)
    bob = conversation_key("tg", 2, 2)
    store.append(alice, "Про кур", "Ответ про кур")
    assert store.load(bob) == []
    store.append(bob, "Про взносы", "Ответ про взносы")
    assert [m["content"] for m in store.load(alice)] == ["Про кур", "Ответ про кур"]


def test_same_id_in_telegram_and_vk_are_different_people(store):
    store.append(conversation_key("tg", 42, 42), "вопрос из телеграма", "ответ")
    assert store.load(conversation_key("vk", 42, 42)) == []


def test_group_chat_keeps_residents_apart(store):
    store.append(conversation_key("vk", 2000000001, 7), "вопрос жителя 7", "ответ")
    assert store.load(conversation_key("vk", 2000000001, 8)) == []


def test_window_drops_old_topics(store, clock):
    conv = conversation_key("vk", 5, 5)
    store.append(conv, "утренний вопрос", "утренний ответ")
    clock.t += 61 * 60
    store.append(conv, "вечерний вопрос", "вечерний ответ")
    assert [m["content"] for m in store.load(conv)] == ["вечерний вопрос", "вечерний ответ"]


def test_only_last_ten_messages_reach_the_model(store):
    conv = conversation_key("tg", 9, 9)
    for i in range(8):
        store.append(conv, f"q{i}", f"a{i}")
    loaded = store.load(conv)
    assert len(loaded) == 10
    assert loaded[0] == {"role": "user", "content": "q3"}
    assert loaded[-1] == {"role": "assistant", "content": "a7"}


def test_long_messages_are_trimmed(store):
    conv = conversation_key("tg", 3, 3)
    store.append(conv, "в" * 5000, "о" * 5000)
    assert all(len(m["content"]) == 1000 for m in store.load(conv))


def test_retention_removes_week_old_messages_on_write(clock):
    backend = MemoryBackend()
    store = HistoryStore(backend, now=clock)
    conv = conversation_key("tg", 4, 4)
    store.append(conv, "старый", "старый ответ")
    clock.t += 8 * 86400
    store.append(conv, "новый", "новый ответ")
    stored = json.loads(backend.get(conv))["messages"]
    assert [m["content"] for m in stored] == ["новый", "новый ответ"]


def test_clear_starts_over(store):
    conv = conversation_key("tg", 6, 6)
    store.append(conv, "про кур", "ответ")
    assert store.clear(conv) is True
    assert store.load(conv) == []


class BrokenBackend:
    def get(self, key):
        raise ConnectionError("storage down")

    put = delete = get


def test_storage_down_means_no_history_and_error_in_log(caplog):
    store = HistoryStore(BrokenBackend())
    conv = conversation_key("tg", 7, 7)
    with caplog.at_level(logging.ERROR):
        assert store.load(conv) == []
        store.append(conv, "q", "a")  # не бросает
        assert store.clear(conv) is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) >= 3
    assert "storage down" in caplog.text


# --------------------------------------------------------------------------- #
# S3Backend против локального HTTP-сервера, повторяющего ответы Object Storage.
# --------------------------------------------------------------------------- #
class FakeS3(BaseHTTPRequestHandler):
    objects: dict = {}
    fail: bool = False
    tokens: list = []

    def log_message(self, *args):
        pass

    def _reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.tokens.append(self.headers.get("X-YaCloud-SubjectToken"))
        if self.fail:
            return self._reply(500, b"internal")
        if self.path not in self.objects:
            return self._reply(404, b"NoSuchKey")
        self._reply(200, self.objects[self.path])

    def do_PUT(self):
        if self.fail:
            return self._reply(500)
        length = int(self.headers["Content-Length"])
        self.objects[self.path] = self.rfile.read(length)
        self._reply(200)

    def do_DELETE(self):
        if self.fail:
            return self._reply(500)
        self.objects.pop(self.path, None)
        self._reply(204)


@pytest.fixture
def fake_s3():
    FakeS3.objects, FakeS3.fail, FakeS3.tokens = {}, False, []
    server = HTTPServer(("127.0.0.1", 0), FakeS3)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_s3_backend_round_trip(fake_s3, clock):
    store = HistoryStore(S3Backend("hist", endpoint=fake_s3, token_fn=lambda: "iam"), now=clock)
    conv = conversation_key("tg", 11, 11)
    assert store.load(conv) == []  # 404 = пустая история, а не ошибка
    store.append(conv, "Можно держать кур?", "Да.")
    assert "/hist/history/tg/11/11.json" in FakeS3.objects
    assert len(store.load(conv)) == 2
    assert set(FakeS3.tokens) == {"iam"}
    assert store.clear(conv) is True
    assert store.load(conv) == []


def test_s3_backend_errors_are_logged_not_raised(fake_s3, caplog):
    store = HistoryStore(S3Backend("hist", endpoint=fake_s3, token_fn=lambda: "iam"))
    FakeS3.fail = True
    with caplog.at_level(logging.ERROR):
        assert store.load(conversation_key("tg", 12, 12)) == []
    assert "HTTP 500" in caplog.text


def test_s3_backend_unreachable_returns_quickly(caplog):
    # Порт 9 на localhost закрыт: соединение отвергается сразу.
    store = HistoryStore(S3Backend("hist", endpoint="http://127.0.0.1:9", token_fn=lambda: "iam"))
    started = time.monotonic()
    with caplog.at_level(logging.ERROR):
        assert store.load(conversation_key("vk", 1, 1)) == []
    assert time.monotonic() - started < 5
    assert "отвечаю без истории" in caplog.text


def test_token_failure_is_storage_failure(caplog):
    def no_token():
        raise OSError("metadata недоступен")

    store = HistoryStore(S3Backend("hist", token_fn=no_token))
    with caplog.at_level(logging.ERROR):
        assert store.load(conversation_key("tg", 1, 1)) == []
    assert "metadata недоступен" in caplog.text
