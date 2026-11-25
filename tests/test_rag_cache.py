"""Дисковый кэш эмбеддингов: пересборка индекса не должна звать API зря."""
from pydantic import PrivateAttr

from bot.search import RulesEngine
from test_dialog import MockEmbedding, make_rules


class CountingEmbedding(MockEmbedding):
    """Мок-эмбеддер со счётчиком: какие тексты реально ушли в векторизацию.

    Считаем только в _get_text_embedding: базовый _get_text_embeddings сам
    циклится по нему, поэтому подсчёт в батч-методе задваивал бы результат.
    """

    _embedded: list = PrivateAttr(default_factory=list)

    def __init__(self):
        super().__init__()
        self._embedded = []

    def _get_text_embedding(self, text):
        self._embedded.append(text)
        return super()._get_text_embedding(text)


def test_rebuild_uses_disk_cache(tmp_path):
    rules = make_rules(tmp_path / "rules.json", ["кур держать можно, до 15 голов"])
    cache = str(tmp_path / "cache.npz")

    first = CountingEmbedding()
    RulesEngine(rules_path=rules, embed_model=first, cache_path=cache)
    assert len(first._embedded) == 1

    # Тот же rules.json, новый эмбеддер: векторы берутся из кэша
    second = CountingEmbedding()
    RulesEngine(rules_path=rules, embed_model=second, cache_path=cache)
    assert second._embedded == []


def test_changed_rules_embed_only_new(tmp_path):
    rules = make_rules(tmp_path / "rules.json", ["кур держать можно, до 15 голов"])
    cache = str(tmp_path / "cache.npz")
    RulesEngine(rules_path=rules, embed_model=CountingEmbedding(), cache_path=cache)

    make_rules(rules, ["кур держать можно, до 15 голов", "взнос ежегодный 5000 рублей"])
    counter = CountingEmbedding()
    RulesEngine(rules_path=rules, embed_model=counter, cache_path=cache)
    assert len(counter._embedded) == 1
    assert "взнос" in counter._embedded[0]


def test_corrupt_cache_starts_empty(tmp_path):
    rules = make_rules(tmp_path / "rules.json", ["кур держать можно, до 15 голов"])
    cache = tmp_path / "cache.npz"
    cache.write_bytes("не npz".encode())

    counter = CountingEmbedding()
    RulesEngine(rules_path=rules, embed_model=counter, cache_path=str(cache))
    assert len(counter._embedded) == 1


def test_model_change_invalidates_cache(tmp_path, monkeypatch):
    rules = make_rules(tmp_path / "rules.json", ["кур держать можно, до 15 голов"])
    cache = str(tmp_path / "cache.npz")
    RulesEngine(rules_path=rules, embed_model=CountingEmbedding(), cache_path=cache)

    # «Другая модель»: метка кэша меняется — кэш сбрасывается целиком
    import bot.search as search

    monkeypatch.setattr(search, "EMBED_MODEL", "other/model")
    counter = CountingEmbedding()
    RulesEngine(rules_path=rules, embed_model=counter, cache_path=cache)
    assert len(counter._embedded) == 1


def test_missing_api_key_leaves_engine_without_index(tmp_path, monkeypatch):
    rules = make_rules(tmp_path / "rules.json", ["кур держать можно, до 15 голов"])
    import bot.search as search

    monkeypatch.setattr(search, "OPENROUTER_API_KEY", "")
    engine = RulesEngine(rules_path=rules, embed_model=None, cache_path=str(tmp_path / "c.npz"))
    assert engine._retriever is None
