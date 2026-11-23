import asyncio
import logging

from aiogram import Bot, Dispatcher

from bot.handlers import router
from bot.vk_bot import run_vk_bot
from config.settings import TELEGRAM_BOT_TOKEN

logging.basicConfig(level=logging.INFO)


async def run_telegram_bot() -> None:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


async def main() -> None:
    await asyncio.gather(
        run_telegram_bot(),
        run_vk_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())
