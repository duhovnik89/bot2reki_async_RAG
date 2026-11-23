import asyncio
import logging
import threading
from pathlib import Path

import aiohttp
from vkbottle import Bot, Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import BotLabeler, Message

from bot.history import conversation_key, default_store
from config.settings import ADMIN_CONTACT, VK_TOKEN

_DOCS_DIR = Path(__file__).parent.parent / "data"

logger = logging.getLogger(__name__)

labeler = BotLabeler()

ABOUT_TEXT = (
    "Я помогаю жителям поселения Междуречье находить ответы на вопросы "
    "по решениям общего собрания. Задайте вопрос — поищу ответ в документах."
)

HELP_TEXT = (
    "Напишите любой вопрос о решениях общего собрания — я найду ответ в документах.\n\n"
    f"Если ответ не найден, свяжитесь с администратором: {ADMIN_CONTACT}"
)

_engine = None
_history = default_store()

RESET_TEXT = "Начинаем с чистого листа: прошлые вопросы я больше не учитываю."


def _main_keyboard() -> str:
    kb = Keyboard(one_time=False)
    kb.add(Text("Договор аренды"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("Памятка по порядку принятия"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("О боте"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("Написать администратору"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


async def _send_doc(message: Message, file_path: Path, title: str, caption: str) -> None:
    upload_server = await message.ctx_api.docs.get_messages_upload_server(
        type="doc", peer_id=message.peer_id
    )
    async with aiohttp.ClientSession() as session:
        with open(file_path, "rb") as f:
            data = aiohttp.FormData()
            data.add_field("file", f, filename=file_path.name)
            async with session.post(upload_server.upload_url, data=data) as resp:
                result = await resp.json()
    saved = await message.ctx_api.docs.save(file=result["file"], title=title)
    doc = saved.doc
    await message.answer(caption, attachment=f"doc{doc.owner_id}_{doc.id}")


@labeler.message()
async def handle_all(message: Message) -> None:
    text = (message.text or "").strip().lower()

    if text in ("начало", "старт", "start"):
        try:
            user_info = await message.ctx_api.users.get(user_ids=[message.from_id])
            name = user_info[0].first_name if user_info else "житель"
        except Exception:
            name = "житель"
        greeting = f"Привет, {name}!\n\nЯ бот поселения Междуречье. {ABOUT_TEXT}\n\n{HELP_TEXT}"
        if await _history.aclear(conversation_key("vk", message.peer_id, message.from_id)):
            greeting += f"\n\n{RESET_TEXT}"
        await message.answer(greeting, keyboard=_main_keyboard())
        return

    if text in ("помощь", "help"):
        await message.answer(HELP_TEXT)
        return

    if text in ("о боте", "about"):
        await message.answer(ABOUT_TEXT)
        return

    if text == "договор аренды":
        await _send_doc(
            message,
            _DOCS_DIR / "Dogovor_arenda_05_2026.docx",
            "Договор аренды",
            "Шаблон договора аренды",
        )
        return

    if text == "памятка по порядку принятия":
        await _send_doc(
            message,
            _DOCS_DIR / "Mezhdurechye_Poryadok_Prinyatiya_v5_2.docx",
            "Памятка по порядку принятия",
            "Памятка для вступающего в поселение",
        )
        return

    if text == "написать администратору":
        await message.answer(f"Напишите администратору: {ADMIN_CONTACT}")
        return

    if not message.text:
        return

    await message.answer("Ищу ответ в документах...")
    conv = conversation_key("vk", message.peer_id, message.from_id)
    answer, ok = await _engine.ask(message.text, await _history.aload(conv))
    await message.answer(answer)
    if ok:
        await _history.aappend(conv, message.text, answer)


def _run_vk_sync() -> None:
    global _engine
    from bot.search import RulesEngine
    _engine = RulesEngine()

    bot = Bot(token=VK_TOKEN)
    bot.labeler.load(labeler)
    logger.info("VK-бот запускается...")
    bot.run_forever()


async def run_vk_bot() -> None:
    if not VK_TOKEN:
        logger.warning("VK_TOKEN не задан — VK-бот не запущен")
        return
    thread = threading.Thread(target=_run_vk_sync, daemon=True)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(1)
