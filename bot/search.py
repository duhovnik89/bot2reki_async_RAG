import asyncio
import logging
import sqlite3
from pathlib import Path

from openai import AsyncOpenAI

from bot import db_source
from config.settings import (
    ADMIN_CONTACT,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
)

logger = logging.getLogger(__name__)

_NEEDS_ADMIN = "[НУЖЕН_АДМИНИСТРАТОР]"

_SHEET_ORDER = [
    "Все решения (хронология)",  # первым — полная история с датами
]


def _load_all_content(db_path: str) -> str:
    if not Path(db_path).exists():
        logger.error("DB not found: %s", db_path)
        return ""

    # mode=ro: на serverless файловая система только для чтения, иначе SQLite
    # пытается создать журнал и падает.
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # Все листы, хронология идёт первой
        all_sheets: list[str] = [
            r[0] for r in con.execute(
                "SELECT DISTINCT sheet FROM rules_raw ORDER BY sheet"
            ).fetchall()
        ]
        ordered = _SHEET_ORDER + [s for s in all_sheets if s not in _SHEET_ORDER]

        parts: list[str] = []
        for sheet in ordered:
            rows = con.execute(
                "SELECT content FROM rules_raw WHERE sheet = ? ORDER BY row_order",
                (sheet,),
            ).fetchall()
            if rows:
                parts.append(f"=== {sheet} ===")
                parts.extend(r[0] for r in rows)

        return "\n\n".join(parts)
    except sqlite3.OperationalError as e:
        logger.error("DB error: %s", e)
        return ""
    finally:
        con.close()


class RulesEngine:
    def __init__(self, db_path: str | None = None) -> None:
        self._client = AsyncOpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
        )
        # Явный путь — фиксированная база; иначе база из db_source (бакет или пакет)
        self._fixed_path = db_path
        path, self._version = (db_path, "fixed") if db_path else db_source.current()
        self._build(path)

    async def _refresh(self) -> None:
        """Перестроить промпт, если в бакете появилась новая версия базы."""
        if self._fixed_path:
            return
        path, version = await asyncio.to_thread(db_source.current)
        if version != self._version:
            await asyncio.to_thread(self._build, path)
            self._version = version
            logger.info("RulesEngine: база обновлена до версии %s", version)

    def _build(self, db_path: str) -> None:
        content = _load_all_content(db_path)
        if not content and getattr(self, "_has_content", False):
            # Новая база не читается — оставляем рабочий промпт, а не пустой
            logger.error("RulesEngine: база %s пустая или битая, оставляю прежнюю", db_path)
            return
        # Статический системный промпт = кэшируемый префикс для DeepSeek
        self._system_prompt = (
            "Ты помощник жителей поселения Междуречье. "
            "Ниже — полный архив решений общих собраний поселения.\n\n"
            "Правила:\n"
            "- Отвечай только на основе документов ниже\n"
            "- Если по одной теме есть несколько решений — действует самое позднее по дате\n"
            "- Если ответ прямо следует из правила в документах (лимит, запрет, "
            "условие) — ответь по этому правилу\n"
            f"- Если ответа в документах нет — ответь строго: {_NEEDS_ADMIN}\n"
            "- Перед вопросом может идти прошлая переписка с этим жителем. Если "
            "вопрос короткий или уточняющий («а если…», «а сколько…»), он про ту же "
            "тему: мысленно дополни его из переписки и ищи ответ в документах по "
            "этой теме. Факты бери только из документов\n"
            "- Отвечай кратко и по делу, на русском языке\n\n"
            f"ДОКУМЕНТЫ ПОСЕЛЕНИЯ:\n\n{content}"
        ) if content else ""
        self._has_content = bool(content)
        logger.info("RulesEngine loaded: %d chars of content", len(content))

    async def answer(self, question: str, history: list[dict] | None = None) -> str:
        text, _ = await self.ask(question, history)
        return text

    async def ask(
        self, question: str, history: list[dict] | None = None
    ) -> tuple[str, bool]:
        """Ответ жителю и признак «ответ настоящий».

        history — прошлые сообщения разговора ([{"role", "content"}]), идут между
        системным промптом и вопросом: база решений остаётся кэшируемым префиксом.
        False — ошибка (база не загружена, DeepSeek упал): такой ответ в историю
        не пишем, чтобы он не сбивал модель в следующих вопросах.
        """
        try:
            await self._refresh()
        except Exception:  # noqa: BLE001 — обновление базы не должно мешать ответу
            logger.exception("RulesEngine: обновление базы упало, отвечаю по прежней")
        if not self._has_content:
            return (
                f"База данных пока не загружена. "
                f"Обратитесь к администратору: {ADMIN_CONTACT}"
            ), False

        try:
            response = await self._client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    *(history or []),
                    {"role": "user", "content": question},
                ],
                temperature=0.1,
                max_tokens=1024,
                # У deepseek-flash рассуждения включены по умолчанию — для
                # чат-бота это медленнее и дороже. Старый deepseek-chat был
                # именно flash без рассуждений.
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception as e:
            logger.error("DeepSeek API error: %s", e)
            return (
                f"Ошибка при обращении к AI. Обратитесь к администратору: {ADMIN_CONTACT}"
            ), False

        answer_text: str = response.choices[0].message.content or ""
        logger.info(
            "DeepSeek raw (история: %d сообщ.): %s", len(history or []), answer_text[:200]
        )

        if _NEEDS_ADMIN in answer_text:
            return (
                "По этому вопросу информации в документах не нашлось. "
                f"Обратитесь к администратору: {ADMIN_CONTACT}"
            ), True

        return answer_text.strip(), True
