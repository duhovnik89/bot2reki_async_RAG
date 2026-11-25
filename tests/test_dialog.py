"""Сквозная цепочка: вопрос → история → RAG-поиск → LLM → ответ, в TG и ВК.

LLM подменён: тест видит, какие сообщения уходят модели. Эмбеддинги тоже
подменены (детерминированный «мешок слов») — без сети и без API-ключей.
"""
import asyncio
import json
import zlib
from pathlib import Path
from types import SimpleNamespace

from llama_index.core.embeddings import BaseEmbedding

import pytest

from bot import handlers, vk_callback
from bot.history import HistoryStore, MemoryBackend, conversation_key
from bot.search import RulesEngine


def make_rules(path, rows):
    """rules.json в формате bot/sync.py: записи хронологии с датами."""
    records = [
        {"sheet": "Все решения (хронология)", "order": i, "date": "01.01.2026", "text": text}
        for i, text in enumerate(rows)
    ]
    Path(path).write_text(
        json.dumps({"records": records}, ensure_ascii=False), encoding="utf-8"
    )
    return str(path)


class MockEmbedding(BaseEmbedding):
    """Слово → crc32 → позиция в векторе: пересечения слов дают близость."""

    def __init__(self):
        super().__init__(model_name="mock")

    def _embed(self, text):
        vector = [0.0] * 64
        for word in text.lower().split():
            vector[zlib.crc32(word.encode()) % 64] = 1.0
        return vector

    def _get_query_embedding(self, query):
        return self._embed(query)

    async def _aget_query_embedding(self, query):
        return self._embed(query)

    def _get_text_embedding(self, text):
        return self._embed(text)


class FakeLLM:
    def __init__(self, replies=None, fail=False):
        self.calls = []
        self.replies = list(replies or [])
        self.fail = fail

    async def achat(self, messages):
        self.calls.append(list(messages))
        if self.fail:
            raise ConnectionError("llm down")
        text = self.replies.pop(0) if self.replies else "ответ"
        return SimpleNamespace(message=SimpleNamespace(content=text))


@pytest.fixture
def engine(tmp_path):
    eng = RulesEngine(
        rules_path=make_rules(tmp_path / "rules.json", ["кур держать можно, до 15 голов"]),
        embed_model=MockEmbedding(),
        cache_path=str(tmp_path / "cache.npz"),
    )
    eng._llm = FakeLLM()
    return eng


def dialog(call):
    """Сообщения без системного промпта: что модель видит из разговора."""
    return [(m.role, m.content) for m in call[1:]]


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# RulesEngine
# --------------------------------------------------------------------------- #
def test_history_goes_between_rules_and_question(engine):
    history = [
        {"role": "user", "content": "Можно держать кур?"},
        {"role": "assistant", "content": "Да, до 15 голов."},
    ]
    text, ok = run(engine.ask("А если их десять?", history))
    call = engine._llm.calls[0]
    assert call[0].role == "system"
    assert "кур держать можно" in call[0].content
    assert dialog(call) == [
        ("user", "Можно держать кур?"),
        ("assistant", "Да, до 15 голов."),
        ("user", "А если их десять?"),
    ]
    assert ok is True


def test_escalation_still_works(engine):
    engine._llm.replies = ["[НУЖЕН_АДМИНИСТРАТОР]"]
    text, ok = run(engine.ask("Сколько стоит электричество?"))
    assert "Обратитесь к администратору" in text
    assert "[НУЖЕН" not in text
    assert ok is True


def test_api_error_is_not_ok(engine):
    engine._llm = FakeLLM(fail=True)
    text, ok = run(engine.ask("вопрос"))
    assert "Ошибка при обращении к AI" in text
    assert ok is False


def test_history_survives_rules_json_update(engine, tmp_path):
    store = HistoryStore(MemoryBackend())
    conv = conversation_key("tg", 1, 1)
    store.append(conv, "Можно держать кур?", "Да, до 15 голов.")
    # rules.json пересобрали из xlsx и подменили
    engine._build(make_rules(tmp_path / "rules_v2.json", ["кур держать можно, до 20 голов"]), "v2")
    run(engine.ask("А если их десять?", store.load(conv)))
    call = engine._llm.calls[0]
    assert "до 20 голов" in call[0].content
    assert ("user", "Можно держать кур?") in dialog(call)


# --------------------------------------------------------------------------- #
# ВКонтакте: воркер и «старт»
# --------------------------------------------------------------------------- #
@pytest.fixture
def vk(engine, monkeypatch):
    sent = []

    async def fake_send(peer_id, text, **kw):
        sent.append((peer_id, text))

    async def fake_users_get(user_ids):
        return [SimpleNamespace(first_name="Анна")]

    store = HistoryStore(MemoryBackend())
    monkeypatch.setattr(vk_callback, "_engine", engine)
    monkeypatch.setattr(vk_callback, "_history", store)
    monkeypatch.setattr(vk_callback, "_send", fake_send)
    monkeypatch.setattr(vk_callback, "_api", SimpleNamespace(users=SimpleNamespace(get=fake_users_get)))
    monkeypatch.setattr(vk_callback, "VK_SECRET", "s")
    return SimpleNamespace(sent=sent, store=store, engine=engine)


def vk_message(peer_id, from_id, text):
    return {
        "type": "message_new",
        "secret": "s",
        "object": {"message": {"peer_id": peer_id, "from_id": from_id, "text": text}},
    }


def test_vk_follow_up_is_about_chickens(vk):
    run(vk_callback.handle_vk_event(vk_message(10, 10, "Можно держать кур?")))
    run(vk_callback.handle_vk_event(vk_message(10, 10, "А если их десять?")))
    second = vk.engine._llm.calls[1]
    assert dialog(second)[0] == ("user", "Можно держать кур?")
    assert dialog(second)[-1] == ("user", "А если их десять?")


def test_vk_two_residents_isolated(vk):
    run(vk_callback.handle_vk_event(vk_message(10, 10, "Можно держать кур?")))
    run(vk_callback.handle_vk_event(vk_message(20, 20, "А если их десять?")))
    assert dialog(vk.engine._llm.calls[1]) == [("user", "А если их десять?")]


def test_vk_start_clears_history(vk):
    run(vk_callback.handle_vk_event(vk_message(10, 10, "Можно держать кур?")))
    run(vk_callback.handle_vk_event(vk_message(10, 10, "старт")))
    assert "чистого листа" in vk.sent[-1][1]
    assert "Привет, Анна" in vk.sent[-1][1]
    run(vk_callback.handle_vk_event(vk_message(10, 10, "А если их десять?")))
    assert dialog(vk.engine._llm.calls[-1]) == [("user", "А если их десять?")]


def test_vk_failed_answer_not_remembered(vk):
    vk.engine._llm.fail = True
    run(vk_callback.handle_vk_event(vk_message(10, 10, "Можно держать кур?")))
    assert vk.store.load(conversation_key("vk", 10, 10)) == []


def test_vk_answers_when_storage_down(vk, monkeypatch, caplog):
    class Broken:
        def get(self, key):
            raise ConnectionError("storage down")

        put = delete = get

    monkeypatch.setattr(vk_callback, "_history", HistoryStore(Broken()))
    run(vk_callback.handle_vk_event(vk_message(10, 10, "Можно держать кур?")))
    assert vk.sent[-1] == (10, "ответ")
    assert "storage down" in caplog.text


# --------------------------------------------------------------------------- #
# Telegram: воркер и /start
# --------------------------------------------------------------------------- #
class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@pytest.fixture
def tg(engine, monkeypatch):
    store = HistoryStore(MemoryBackend())
    monkeypatch.setattr(handlers, "_engine", engine)
    monkeypatch.setattr(handlers, "_history", store)
    return SimpleNamespace(store=store, engine=engine, bot=FakeBot())


def tg_job(chat_id, user_id, text):
    return {
        "target": "tg",
        "kind": "question",
        "chat_id": chat_id,
        "conv": conversation_key("tg", chat_id, user_id),
        "text": text,
    }


def test_tg_follow_up_is_about_chickens(tg):
    run(handlers.process_tg_job(tg_job(1, 1, "Можно держать кур?"), tg.bot))
    run(handlers.process_tg_job(tg_job(1, 1, "А если их десять?"), tg.bot))
    assert dialog(tg.engine._llm.calls[1])[0] == ("user", "Можно держать кур?")
    assert tg.bot.sent[-1] == (1, "ответ")


def test_tg_two_residents_isolated(tg):
    run(handlers.process_tg_job(tg_job(1, 1, "Можно держать кур?"), tg.bot))
    run(handlers.process_tg_job(tg_job(2, 2, "А если их десять?"), tg.bot))
    assert dialog(tg.engine._llm.calls[1]) == [("user", "А если их десять?")]


def test_tg_job_without_conv_still_answers(tg):
    job = tg_job(1, 1, "вопрос")
    del job["conv"]  # задание, поставленное старой версией
    run(handlers.process_tg_job(job, tg.bot))
    assert tg.bot.sent == [(1, "ответ")]


def test_tg_start_clears_history_and_keeps_buttons(tg):
    run(handlers.process_tg_job(tg_job(1, 1, "Можно держать кур?"), tg.bot))
    replies = []

    async def answer(text, **kw):
        replies.append((text, kw))

    message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        from_user=SimpleNamespace(id=1, first_name="Иван"),
        answer=answer,
    )
    run(handlers.cmd_start(message))
    text, kw = replies[0]
    assert "Привет, Иван" in text and "чистого листа" in text
    assert kw["reply_markup"] is not None
    assert tg.store.load(conversation_key("tg", 1, 1)) == []
