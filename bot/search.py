"""RulesEngine: RAG-поиск по архиву решений (LlamaIndex) + ответ через LLM.

Подготовка: bot/sync.py превращает Excel в rules.json (плоский список записей
«лист — дата — текст»). При старте бот режет записи на фрагменты, векторизует
их и строит в памяти VectorStoreIndex (LlamaIndex); на каждый вопрос векторным
поиском достаются топ-N ближайших фрагментов, и только они вместе с правилами
уходят в LLM. SQLite больше нет вообще.

И генерация, и эмбеддинги ходят через OpenRouter API (OpenAI-совместимый):
LLM — класс OpenRouter (llama-index-llms-openrouter, модель google/gemini-3.1-
flash-lite), эмбеддинги — класс OpenAILikeEmbedding (модель intfloat/
multilingual-e5-large, префиксы «query: »/«passage: » дописываем сами).

Дисковый кэш эмбеддингов (rules.embeddings.npz): векторы фрагментов хранятся
по хэшу их текста, поэтому после первого запуска индекс собирается без вызовов
API (эмбеддится только вопрос жителя), а изменённые/новые записи доэмбеддируются
точечно. Смена модели или сервера эмбеддингов сбрасывает кэш целиком.

Почему так.
  - Записи Excel — готовые смысловые единицы (тема + решение), поэтому фрагмент
    по умолчанию = запись целиком; SentenceSplitter режет только длинные записи,
    чтобы короткие не перемалывались и поиск не рвал решения посередине.
  - Уточняющий вопрос («а если их десять?») сам по себе не ищется, поэтому
    векторный запрос дополняется последними репликами жителя из истории.
  - Хронология и тематические листы пересекаются: дубли выкидываются уже после
    поиска, слоты в промпте не тратятся.
  - Эмбеддер, LLM и индекс общие для всех RulesEngine процесса (Telegram- и
    VK-движки живут в одном процессе) — тяжёлое делается один раз.
  - Любой отказ на этапе построения индекса (сеть, ключ, бакет) не роняет бот
    и не «запоминается»: на следующем вопросе построение повторяется.
"""

import asyncio
import hashlib
import json
import logging
import os
import threading
from pathlib import Path

import numpy as np
from llama_index.core import VectorStoreIndex
from llama_index.core.llms import ChatMessage
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.postprocessor import LongContextReorder
from llama_index.core.schema import TextNode
from llama_index.embeddings.openai_like import OpenAILikeEmbedding
from llama_index.llms.openrouter import OpenRouter
from pydantic import PrivateAttr

from bot import source
from config.settings import (
    ADMIN_CONTACT,
    EMBED_MODEL,
    LLM_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    RAG_CHUNK_OVERLAP,
    RAG_CHUNK_SIZE,
    RAG_CONTEXT_MAX_CHARS,
    RAG_EMBED_BATCH_SIZE,
    RAG_EMBEDDINGS_CACHE,
    RAG_EMBED_PASSAGE_PREFIX,
    RAG_EMBED_QUERY_PREFIX,
    RAG_TOP_K,
)

logger = logging.getLogger(__name__)

_NEEDS_ADMIN = "[НУЖЕН_АДМИНИСТРАТОР]"

# Записи короче этого (символов) не режем вовсе — SentenceSplitter перемолол бы
# их форматирование. Длинные режутся на куски ~RAG_CHUNK_SIZE токенов.
_SPLIT_THRESHOLD = RAG_CHUNK_SIZE * 2

# --- общие для процесса: эмбеддер, LLM и индекс (см. docstring модуля) ---
_embed_model = None
_llm = None
_clients_lock = threading.Lock()
_index_cache: dict = {"version": None, "retriever": None}
_index_lock = threading.Lock()

_reorder = LongContextReorder()


class PrefixedOpenAIEmbedding(OpenAILikeEmbedding):
    """OpenAI-совместимый эмбеддер с префиксами запроса/документа.

    OpenAILikeEmbedding отправляет текст как есть, а e5-модели обучены с
    префиксами «query: »/«passage: » — без них поиск заметно хуже. Дописываем
    их сами, ровно как это делал локальный HuggingFaceEmbedding.
    """

    _query_prefix: str = PrivateAttr()
    _text_prefix: str = PrivateAttr()

    def __init__(self, query_prefix: str, text_prefix: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._query_prefix = query_prefix
        self._text_prefix = text_prefix

    def _get_query_embedding(self, query: str) -> list[float]:
        return super()._get_query_embedding(self._query_prefix + query)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return await super()._aget_query_embedding(self._query_prefix + query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return super()._get_text_embedding(self._text_prefix + text)

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return super()._get_text_embeddings([self._text_prefix + t for t in texts])


def _get_embed_model():
    """Эмбеддер на процесс. None — ключ не задан (индекс построить нельзя)."""
    global _embed_model
    with _clients_lock:
        if _embed_model is None:
            if not OPENROUTER_API_KEY:
                logger.error(
                    "OPENROUTER_API_KEY не задан — векторизация недоступна, "
                    "индекс решений не построить"
                )
                return None
            logger.info("Эмбеддинги: %s, модель %s", OPENROUTER_BASE_URL, EMBED_MODEL)
            _embed_model = PrefixedOpenAIEmbedding(
                query_prefix=RAG_EMBED_QUERY_PREFIX,
                text_prefix=RAG_EMBED_PASSAGE_PREFIX,
                model_name=EMBED_MODEL,
                api_base=OPENROUTER_BASE_URL,
                api_key=OPENROUTER_API_KEY,
                embed_batch_size=RAG_EMBED_BATCH_SIZE,
                max_retries=3,
                timeout=60.0,
            )
        return _embed_model


def _get_llm():
    """LLM на процесс. None — ключ не задан (ответить нечем)."""
    global _llm
    with _clients_lock:
        if _llm is None:
            if not OPENROUTER_API_KEY:
                logger.error(
                    "OPENROUTER_API_KEY не задан — генерация ответов недоступна"
                )
                return None
            logger.info("LLM: %s, модель %s", OPENROUTER_BASE_URL, LLM_MODEL)
            _llm = OpenRouter(
                model=LLM_MODEL,
                api_key=OPENROUTER_API_KEY,
                api_base=OPENROUTER_BASE_URL,
                temperature=0.1,
                max_tokens=1024,
                max_retries=3,
            )
        return _llm


def _file_version(path: str) -> str:
    """Версия фиксированного файла по содержимому — ключ кэша индекса."""
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except OSError:
        return "missing"


def _load_records(path: str) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        records = [
            r
            for r in data.get("records", [])
            if isinstance(r, dict) and str(r.get("text", "")).strip()
        ]
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error("rules.json не читается (%s): %s", path, e)
        return []
    logger.info("rules.json: %d записей из %s", len(records), path)
    return records


def _node_text(record: dict) -> str:
    if record.get("date"):
        head = f"Решение общего собрания от {record['date']}."
    else:
        head = f"Раздел «{record.get('sheet', '')}»."
    return f"{head}\n{str(record['text']).strip()}"


def _make_nodes(records: list[dict]) -> list[TextNode]:
    splitter = SentenceSplitter(
        chunk_size=RAG_CHUNK_SIZE, chunk_overlap=RAG_CHUNK_OVERLAP
    )
    nodes: list[TextNode] = []
    for record in records:
        text = _node_text(record)
        pieces = splitter.split_text(text) if len(text) > _SPLIT_THRESHOLD else [text]
        for piece in pieces:
            nodes.append(TextNode(text=piece))
    return nodes


# --------------------------------------------------------------------------- #
# Дисковый кэш эмбеддингов
# --------------------------------------------------------------------------- #

def _cache_meta() -> str:
    """Метка кэша: векторы годятся только от той же модели на том же сервере."""
    return f"{OPENROUTER_BASE_URL}|{EMBED_MODEL}"


def _text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _load_embedding_cache(cache_path: str) -> dict[str, np.ndarray]:
    try:
        data = np.load(cache_path, allow_pickle=False)
        if str(data["meta"]) != _cache_meta():
            logger.info("Кэш эмбеддингов от другой модели/сервера — пересчитываю")
            return {}
        vectors = data["vectors"]
        return {str(h): vectors[i] for i, h in enumerate(data["hashes"])}
    except Exception:  # noqa: BLE001 — файла нет или битый: начинаем с пустого
        return {}


def _save_embedding_cache(cache_path: str, store: dict[str, np.ndarray]) -> None:
    tmp = cache_path + ".part"
    try:
        Path(tmp).parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as f:  # np.savez добавил бы .npz к имени .part
            np.savez_compressed(
                f,
                hashes=np.array(list(store.keys())),
                vectors=np.array(list(store.values()), dtype=np.float32),
                meta=np.array(_cache_meta()),
            )
        os.replace(tmp, cache_path)
    except OSError as e:
        logger.error("Кэш эмбеддингов не сохранён (%s): %s", cache_path, e)


def _attach_embeddings(nodes: list[TextNode], embed, cache_path: str) -> None:
    """Проставить node.embedding: из кэша, недостающие — запросом к API.

    VectorStoreIndex пересчитывает вектор только для узлов без embedding,
    поэтому заполненные узлы не стоят ни одного вызова.
    """
    store = _load_embedding_cache(cache_path)
    missing = [n for n in nodes if _text_hash(n.get_content()) not in store]
    logger.info(
        "Эмбеддинги: %d из %d фрагментов в кэше, запрашиваю %d",
        len(nodes) - len(missing), len(nodes), len(missing),
    )
    if missing:
        texts = [n.get_content() for n in missing]
        vectors = embed.get_text_embedding_batch(texts)
        for node, vector in zip(missing, vectors):
            node.embedding = vector
            store[_text_hash(node.get_content())] = np.asarray(vector, dtype=np.float32)
        _save_embedding_cache(cache_path, store)
    for node in nodes:
        if node.embedding is None:
            node.embedding = store[_text_hash(node.get_content())].tolist()


def _body(text: str) -> str:
    """Текст фрагмента без строки-заголовка — для сравнения дублей."""
    return text.split("\n", 1)[-1].strip().lower()


def _dedupe(nodes: list) -> list:
    seen: set[str] = set()
    out = []
    for n in nodes:
        key = _body(n.node.get_content())[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def _search_query(question: str, history: list[dict] | None) -> str:
    """Запрос для векторного поиска: уточняющий вопрос дополняем недавними
    репликами жителя — «а если их десять?» без «можно держать кур?» не ищется."""
    parts = [
        m["content"]
        for m in (history or [])
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)
    ][-2:]
    parts.append(question)
    return "\n".join(parts)[-1500:]


class RulesEngine:
    def __init__(
        self,
        rules_path: str | None = None,
        embed_model=None,
        cache_path: str | None = None,
    ) -> None:
        # Явные пути — для тестов и отладки; иначе файл из source
        # (бакет или пакет) и кэш из настроек
        self._fixed_path = rules_path
        self._embed_override = embed_model
        self._cache_path = cache_path or RAG_EMBEDDINGS_CACHE
        self._version = ""
        self._retriever = None
        self._llm = _get_llm()
        if rules_path:
            self._build(rules_path, _file_version(rules_path))
        else:
            path, version = source.current()
            self._build(path, version)

    def _build(self, path: str, version: str) -> None:
        """Построить индекс из rules.json.

        Отказ (сеть, ключ, битый файл) не роняет бот: остаётся прежний индекс.
        Версия фиксируется только при успешной сборке или при заведомо пустом
        файле — упавшую сборку повторяем на следующем вопросе.
        """
        records = _load_records(path)
        if not records:
            if self._retriever is not None:
                logger.error("RulesEngine: %s пустой или битый, оставляю прежний индекс", path)
            else:
                self._retriever = None
            self._version = version
            return

        with _index_lock:
            # Чужой движок уже построил индекс этой версии — берём готовый
            if self._embed_override is None and _index_cache["version"] == version:
                self._retriever = _index_cache["retriever"]
                self._version = version
                return

            embed = self._embed_override or _get_embed_model()
            if embed is None:
                return
            try:
                nodes = _make_nodes(records)
                _attach_embeddings(nodes, embed, self._cache_path)
                index = VectorStoreIndex(nodes, embed_model=embed)
                retriever = index.as_retriever(similarity_top_k=RAG_TOP_K)
            except Exception:  # noqa: BLE001 — попробуем снова на следующем вопросе
                logger.exception("RulesEngine: построение индекса упало")
                return

            if self._embed_override is None:
                _index_cache.update(version=version, retriever=retriever)
            self._retriever = retriever
            self._version = version
            logger.info("RulesEngine: индекс построен, %d фрагментов", len(nodes))

    async def _refresh(self) -> None:
        """Перестроить индекс, если в бакете появилась новая версия файла.

        Фиксированный путь (тесты, отладка) не проверяется — как и раньше.
        """
        if self._fixed_path:
            return
        path, version = await asyncio.to_thread(source.current)
        if version != self._version:
            await asyncio.to_thread(self._build, path, version)

    def _retrieve_context(self, question: str, history: list[dict] | None) -> str:
        query = _search_query(question, history)
        with _index_lock:
            nodes = self._retriever.retrieve(query)
        nodes = _reorder.postprocess_nodes(_dedupe(nodes))
        context = "\n\n".join(
            f"[{i}] {n.node.get_content()}" for i, n in enumerate(nodes, 1)
        )
        if len(context) > RAG_CONTEXT_MAX_CHARS:
            context = context[:RAG_CONTEXT_MAX_CHARS]
        top_score = max((n.score or 0.0) for n in nodes) if nodes else 0.0
        logger.info(
            "RAG: запрос %d симв., фрагментов %d, лучшая близость %.2f, контекст %d симв.",
            len(query), len(nodes), top_score, len(context),
        )
        return context

    async def answer(self, question: str, history: list[dict] | None = None) -> str:
        text, _ = await self.ask(question, history)
        return text

    async def ask(
        self, question: str, history: list[dict] | None = None
    ) -> tuple[str, bool]:
        """Ответ жителю и признак «ответ настоящий».

        history — прошлые сообщения разговора ([{"role", "content"}]), идут между
        системным промптом и вопросом. False — ошибка (база не загружена, поиск
        или LLM упал): такой ответ в историю не пишем, чтобы он не сбивал модель
        в следующих вопросах.
        """
        try:
            await self._refresh()
        except Exception:  # noqa: BLE001 — обновление базы не должно мешать ответу
            logger.exception("RulesEngine: обновление базы упало, отвечаю по прежней")
        if self._retriever is None:
            return (
                f"База решений пока не загружена. "
                f"Обратитесь к администратору: {ADMIN_CONTACT}"
            ), False

        try:
            context = await asyncio.to_thread(
                self._retrieve_context, question, history
            )
        except Exception:  # noqa: BLE001 — жителю нужен ответ, а не трассировка
            logger.exception("RulesEngine: поиск по индексу упал")
            return (
                f"Ошибка при обращении к AI. Обратитесь к администратору: {ADMIN_CONTACT}"
            ), False

        system_prompt = (
            "Ты помощник жителей поселения Междуречье. "
            "Ниже — фрагменты архива решений общих собраний, наиболее близкие "
            "к вопросу. Если ответа во фрагментах нет, он может отсутствовать "
            "и в архиве целиком.\n\n"
            "Правила:\n"
            "- Отвечай только на основе фрагментов ниже\n"
            "- Если по одной теме есть несколько решений — действует самое позднее по дате\n"
            "- Если ответ прямо следует из правила во фрагментах (лимит, запрет, "
            "условие) — ответь по этому правилу\n"
            f"- Если ответа во фрагментах нет — ответь строго: {_NEEDS_ADMIN}\n"
            "- Перед вопросом может идти прошлая переписка с этим жителем. Если "
            "вопрос короткий или уточняющий («а если…», «а сколько…»), он про ту же "
            "тему: мысленно дополни его из переписки и ищи ответ во фрагментах по "
            "этой теме. Факты бери только из фрагментов\n"
            "- Отвечай кратко и по делу, на русском языке\n\n"
            f"ФРАГМЕНТЫ АРХИВА РЕШЕНИЙ:\n\n{context}"
        )

        if self._llm is None:
            return (
                f"Ошибка при обращении к AI. Обратитесь к администратору: {ADMIN_CONTACT}"
            ), False
        messages = [
            ChatMessage(role="system", content=system_prompt),
            *(
                ChatMessage(role=m["role"], content=m["content"])
                for m in (history or [])
                if isinstance(m, dict) and m.get("role") and isinstance(m.get("content"), str)
            ),
            ChatMessage(role="user", content=question),
        ]

        try:
            response = await self._llm.achat(messages)
        except Exception as e:
            logger.error("LLM API error: %s", e)
            return (
                f"Ошибка при обращении к AI. Обратитесь к администратору: {ADMIN_CONTACT}"
            ), False

        content = response.message.content
        if isinstance(content, str):
            answer_text: str = content
        else:  # блоки (бывает у мультимодальных ответов) — собираем текстовые
            answer_text = "".join(getattr(b, "text", "") for b in content)
        logger.info(
            "LLM raw (история: %d сообщ.): %s", len(history or []), answer_text[:200]
        )

        if _NEEDS_ADMIN in answer_text:
            return (
                "По этому вопросу информации в документах не нашлось. "
                f"Обратитесь к администратору: {ADMIN_CONTACT}"
            ), True

        return answer_text.strip(), True
