"""ВКонтакте Callback API для serverless (Yandex Cloud Functions).

Ключевая идея — разрыв «ответить VK» и «посчитать ответ»: VK ждёт `ok` всего
несколько секунд, а DeepSeek с полным архивом в промпте отвечает дольше.
Медленные ветки (вопрос к DeepSeek, отправка документов) уходят в асинхронный
само-вызов функции. Сам механизм живёт в `bot/async_jobs.py` — он общий с
Telegram.
"""
import logging
import random
from pathlib import Path

import aiohttp
from vkbottle import API, Keyboard, KeyboardButtonColor, Text

from bot.async_jobs import enqueue
from bot.history import conversation_key, default_store
from bot.search import RulesEngine
from config.settings import (
    ADMIN_CONTACT,
    VK_CONFIRMATION,
    VK_SECRET,
    VK_TOKEN,
)

logger = logging.getLogger(__name__)

_DOCS_DIR = Path(__file__).parent.parent / "data"

# Создаются один раз на холодном старте и переживают тёплые вызовы.
_api = API(token=VK_TOKEN)
_engine = RulesEngine()
_history = default_store()

RESET_TEXT = "Начинаем с чистого листа: прошлые вопросы я больше не учитываю."

_OK = {"statusCode": 200, "body": "ok"}

ABOUT_TEXT = (
    "Я помогаю жителям поселения Междуречье находить ответы на вопросы "
    "по решениям общего собрания. Задайте вопрос — поищу ответ в документах."
)

HELP_TEXT = (
    "Напишите любой вопрос о решениях общего собрания — я найду ответ в документах.\n\n"
    f"Если ответ не найден, свяжитесь с администратором: {ADMIN_CONTACT}"
)

# Документы: ключ кнопки → (файл, заголовок, подпись)
_DOCS = {
    "договор аренды": (
        "Dogovor_arenda_05_2026.docx",
        "Договор аренды",
        "Шаблон договора аренды",
    ),
    "памятка по порядку принятия": (
        "Mezhdurechye_Poryadok_Prinyatiya_v5_2.docx",
        "Памятка по порядку принятия",
        "Памятка для вступающего в поселение",
    ),
}


def _main_keyboard() -> str:
    kb = Keyboard(one_time=False)
    kb.add(Text("Договор аренды"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("Памятка по порядку принятия"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("О боте"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("Написать администратору"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


async def _send(peer_id: int, text: str, **kw) -> None:
    await _api.messages.send(
        peer_id=peer_id, message=text, random_id=random.randint(1, 2**31 - 1), **kw
    )


async def _send_doc(peer_id: int, filename: str, title: str, caption: str) -> None:
    file_path = _DOCS_DIR / filename
    upload_server = await _api.docs.get_messages_upload_server(type="doc", peer_id=peer_id)
    async with aiohttp.ClientSession() as session:
        with open(file_path, "rb") as f:
            data = aiohttp.FormData()
            data.add_field("file", f, filename=file_path.name)
            async with session.post(upload_server.upload_url, data=data) as resp:
                result = await resp.json()
    saved = await _api.docs.save(file=result["file"], title=title)
    doc = saved.doc
    await _send(peer_id, caption, attachment=f"doc{doc.owner_id}_{doc.id}")


# --------------------------------------------------------------------------- #
# Синхронный путь — вызывается из HTTP-обработчика (API Gateway → функция).
# Возвращает ответ VK быстро. Медленное уходит в async-воркер через enqueue.
# --------------------------------------------------------------------------- #
async def handle_vk_event(data: dict) -> dict:
    # 1. Подтверждение адреса сервера
    if data.get("type") == "confirmation":
        return {"statusCode": 200, "body": VK_CONFIRMATION}

    # 2. Проверка секрета
    if VK_SECRET and data.get("secret") != VK_SECRET:
        return {"statusCode": 403, "body": "forbidden"}

    if data.get("type") != "message_new":
        return _OK

    msg = data["object"]["message"]
    peer_id = msg["peer_id"]
    from_id = msg.get("from_id", peer_id)
    text = (msg.get("text") or "").strip()
    low = text.lower()
    conv = conversation_key("vk", peer_id, from_id)

    # --- быстрые статические ветки: отвечаем сразу, в окно VK укладываемся ---
    if low in ("начало", "старт", "start"):
        try:
            users = await _api.users.get(user_ids=[from_id])
            name = users[0].first_name if users else "житель"
        except Exception:  # noqa: BLE001
            name = "житель"
        greeting = f"Привет, {name}!\n\nЯ бот поселения Междуречье. {ABOUT_TEXT}\n\n{HELP_TEXT}"
        if await _history.aclear(conv):
            greeting += f"\n\n{RESET_TEXT}"
        await _send(peer_id, greeting, keyboard=_main_keyboard())
        return _OK

    if low in ("помощь", "help"):
        await _send(peer_id, HELP_TEXT)
        return _OK

    if low in ("о боте", "about"):
        await _send(peer_id, ABOUT_TEXT)
        return _OK

    if low == "написать администратору":
        await _send(peer_id, f"Напишите администратору: {ADMIN_CONTACT}")
        return _OK

    if not text:
        return _OK

    # --- медленные ветки: документы и вопрос к DeepSeek → в async-воркер ---
    if low in _DOCS:
        job = {"target": "vk", "kind": "doc", "peer_id": peer_id, "doc": low}
    else:
        job = {
            "target": "vk",
            "kind": "question",
            "peer_id": peer_id,
            "conv": conv,
            "text": text,
        }

    await enqueue(job, lambda: process_vk_job(job))
    return _OK


# --------------------------------------------------------------------------- #
# Асинхронный воркер — вызывается само-инвоком (вне окна ожидания VK).
# --------------------------------------------------------------------------- #
async def process_vk_job(job: dict) -> None:
    kind = job.get("kind")
    peer_id = job["peer_id"]

    if kind == "doc":
        filename, title, caption = _DOCS[job["doc"]]
        await _send_doc(peer_id, filename, title, caption)
        return

    if kind == "question":
        # Задание, поставленное до выкатки истории, ключа conv не несёт.
        conv = job.get("conv") or conversation_key("vk", peer_id, peer_id)
        history = await _history.aload(conv)
        logger.info("VK-воркер: иду в DeepSeek для %s (история: %d)", peer_id, len(history))
        answer, ok = await _engine.ask(job["text"], history)
        await _send(peer_id, answer)
        if ok:
            await _history.aappend(conv, job["text"], answer)
        return

    logger.warning("Неизвестный тип задания: %s", kind)
