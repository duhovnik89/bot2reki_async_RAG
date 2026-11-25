import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.async_jobs import enqueue
from bot.history import conversation_key, default_store
from bot.search import RulesEngine
from config.settings import ADMIN_CONTACT

logger = logging.getLogger(__name__)

router = Router()
_engine = RulesEngine()
_history = default_store()

RESET_TEXT = "Начинаем с чистого листа: прошлые вопросы я больше не учитываю."


def _conv(message: Message) -> str:
    user_id = message.from_user.id if message.from_user else message.chat.id
    return conversation_key("tg", message.chat.id, user_id)

_DOCS_DIR = Path(__file__).parent.parent / "data"

# Telegram отбрасывает сообщения длиннее 4096 символов
_MESSAGE_LIMIT = 4096

# Бюджет на отправку подтверждения. Оно необязательное: ответ придёт из воркера
# в любом случае. Лимит сессии (15 с) для синхронной ветки слишком щедрый —
# вместе с постановкой задания это выходит за окно ожидания Telegram.
_ACK_TIMEOUT = 5


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="О боте", callback_data="about"),
                InlineKeyboardButton(text="Помощь", callback_data="help"),
            ],
            [
                InlineKeyboardButton(text="Договор аренды", callback_data="doc_lease"),
                InlineKeyboardButton(text="Памятка по порядку принятия", callback_data="doc_guide"),
            ],
            [
                InlineKeyboardButton(
                    text="Написать администратору", callback_data="admin"
                ),
            ],
        ]
    )


ABOUT_TEXT = (
    "Я помогаю жителям поселения Междуречье находить ответы на вопросы "
    "по решениям общего собрания. Задайте вопрос — поищу ответ в документах."
)

HELP_TEXT = (
    "Доступные команды:\n\n"
    "/start — начало работы\n"
    "/help — список команд\n"
    "/about — о боте\n\n"
    "Или просто напишите свой вопрос."
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    name = message.from_user.first_name if message.from_user else "житель"
    text = f"Привет, {name}!\n\nЯ бот поселения Междуречье. {ABOUT_TEXT}"
    if await _history.aclear(_conv(message)):
        text += f"\n\n{RESET_TEXT}"
    await message.answer(text, reply_markup=_main_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("about"))
async def cmd_about(message: Message) -> None:
    await message.answer(ABOUT_TEXT)


@router.callback_query(F.data == "about")
async def cb_about(callback: CallbackQuery) -> None:
    await callback.message.answer(ABOUT_TEXT)
    await callback.answer()


@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery) -> None:
    await callback.message.answer(HELP_TEXT)
    await callback.answer()


@router.callback_query(F.data == "admin")
async def cb_admin(callback: CallbackQuery) -> None:
    await callback.message.answer(f"Напишите администратору: {ADMIN_CONTACT}")
    await callback.answer()


@router.callback_query(F.data == "doc_lease")
async def cb_doc_lease(callback: CallbackQuery) -> None:
    doc = FSInputFile(_DOCS_DIR / "Dogovor_arenda_05_2026.docx")
    await callback.message.answer_document(doc, caption="Шаблон договора аренды")
    await callback.answer()


@router.callback_query(F.data == "doc_guide")
async def cb_doc_guide(callback: CallbackQuery) -> None:
    doc = FSInputFile(_DOCS_DIR / "Mezhdurechye_Poryadok_Prinyatiya_v5_2.docx")
    await callback.message.answer_document(doc, caption="Памятка для вступающего в поселение")
    await callback.answer()


@router.message(F.text)
async def handle_text(message: Message) -> None:
    """Быстрая часть: подтвердить приём и отдать вопрос в async-воркер.

    Поход в LLM может занимать десятки секунд и в окно ожидания Telegram не
    укладывается. Здесь остаётся только отправка "Ищу ответ..." — функция
    успевает вернуть шлюзу 200 за доли секунды, и Telegram не уходит в ретраи.
    """
    logger.info("Вопрос из чата %s — отправляю подтверждение", message.chat.id)
    # Подтверждение отправляем по возможности. Замер 23.09.2026: api.telegram.org
    # из функции иногда не отвечает вовсе, и раньше это валило весь обработчик
    # наружу как `502 Error during function invocation` — вопрос терялся целиком.
    # Теперь отказ здесь стоит только самого подтверждения.
    try:
        await asyncio.wait_for(
            message.answer("Ищу ответ в документах..."), timeout=_ACK_TIMEOUT
        )
        logger.info("Подтверждение отправлено, ставлю задание в очередь")
    except Exception:  # noqa: BLE001
        logger.warning(
            "Подтверждение отправить не удалось — api.telegram.org не ответил за %s с",
            _ACK_TIMEOUT,
            exc_info=True,
        )

    job = {
        "target": "tg",
        "kind": "question",
        "chat_id": message.chat.id,
        "conv": _conv(message),
        "text": message.text,
    }
    bot = message.bot
    await enqueue(job, lambda: process_tg_job(job, bot))


# --------------------------------------------------------------------------- #
# Асинхронный воркер — вызывается само-инвоком вне окна ожидания Telegram.
# --------------------------------------------------------------------------- #
async def _send_long(bot: Bot, chat_id: int, text: str) -> None:
    text = text or "Ответ оказался пустым. Обратитесь к администратору: " + ADMIN_CONTACT
    for start in range(0, len(text), _MESSAGE_LIMIT):
        await bot.send_message(chat_id, text[start : start + _MESSAGE_LIMIT])


async def process_tg_job(job: dict, bot: Bot) -> None:
    """В async-ветке ретраев Telegram уже нет: упавшее задание никто не повторит,
    поэтому об отказе жителя надо известить явно."""
    chat_id = job["chat_id"]

    if job.get("kind") != "question":
        logger.warning("Неизвестный тип задания: %s", job.get("kind"))
        return

    try:
        # Задание, поставленное до выкатки истории, ключа conv не несёт.
        conv = job.get("conv") or conversation_key("tg", chat_id, chat_id)
        history = await _history.aload(conv)
        logger.info("Воркер: иду в LLM для чата %s (история: %d)", chat_id, len(history))
        answer, ok = await _engine.ask(job["text"], history)
        logger.info("Воркер: ответ получен (%s символов), отправляю", len(answer))
        await _send_long(bot, chat_id, answer)
        logger.info("Воркер: ответ доставлен в чат %s", chat_id)
        if ok:
            await _history.aappend(conv, job["text"], answer)
    except Exception:  # noqa: BLE001
        logger.exception("Задание для чата %s упало", chat_id)
        try:
            await bot.send_message(
                chat_id,
                "Не получилось найти ответ — что-то сломалось на моей стороне. "
                f"Напишите администратору: {ADMIN_CONTACT}",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Не смог отправить сообщение об ошибке в чат %s", chat_id)
