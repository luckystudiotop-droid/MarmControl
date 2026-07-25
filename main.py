import asyncio
import logging
import os
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions

# -------------------------------------------------------------------
# 1. Настройки и Переменные
# -------------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
# Render автоматически передает PORT (по умолчанию 10000 или 8080)
PORT = int(os.getenv("PORT", 8080))

bot = Bot(token=TOKEN)
dp = Dispatcher()
db_pool = None


# -------------------------------------------------------------------
# 2. Веб-сервер для "Health Check" (Чтобы Render не убивал бота)
# -------------------------------------------------------------------
async def handle_ping(request):
    return web.Response(text="MarmControl Bot is ALIVE!", status=200)


async def run_health_check_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"🌐 Health-check сервер запущен на порту {PORT}")


# -------------------------------------------------------------------
# 3. База данных (Neon PostgreSQL)
# -------------------------------------------------------------------
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS commands (
                chat_id BIGINT,
                command_name TEXT,
                message_id BIGINT,
                PRIMARY KEY (chat_id, command_name)
            )
        """)
    logging.info("🗄 База данных успешно подключена!")


# -------------------------------------------------------------------
# 4. Вспомогательные функции
# -------------------------------------------------------------------
async def is_admin(chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in ("administrator", "creator")


def get_target_user(message: types.Message):
    if not message.reply_to_message:
        return None
    return message.reply_to_message.from_user


# -------------------------------------------------------------------
# 5. Системные команды модерации
# -------------------------------------------------------------------
@dp.message(Command("bot"))
async def cmd_bot(message: types.Message):
    await message.answer("🤖 Бот-модератор работает 24/7 в облаке!")


@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.answer("❌ Нет прав.")
    target = get_target_user(message)
    if not target:
        return await message.answer("⚠️ Ответьте на сообщение пользователя.")
    try:
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=target.id)
        await message.answer(f"⛔ Пользователь {target.full_name} забанен.")
    except Exception as e:
        await message.answer(f"Ошибка при бане: {e}")


@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.answer("❌ Нет прав.")
    target = get_target_user(message)
    if not target:
        return await message.answer("⚠️ Ответьте на сообщение пользователя.")
    try:
        await bot.unban_chat_member(chat_id=message.chat.id, user_id=target.id, only_if_banned=True)
        await message.answer(f"✅ Пользователь {target.full_name} разбанен.")
    except Exception as e:
        await message.answer(f"Ошибка при разбане: {e}")


@dp.message(Command("mute"))
async def cmd_mute(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.answer("❌ Нет прав.")
    target = get_target_user(message)
    if not target:
        return await message.answer("⚠️ Ответьте на сообщение пользователя.")
    try:
        await bot.restrict_chat_member(message.chat.id, target.id, ChatPermissions(can_send_messages=False))
        await message.answer(f"🔇 Пользователь {target.full_name} заглушен.")
    except Exception as e:
        await message.answer(f"Ошибка при муте: {e}")


@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.answer("❌ Нет прав.")
    target = get_target_user(message)
    if not target:
        return await message.answer("⚠️ Ответьте на сообщение пользователя.")
    try:
        perms = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True)
        await bot.restrict_chat_member(message.chat.id, target.id, perms)
        await message.answer(f"🔊 Размут для {target.full_name}.")
    except Exception as e:
        await message.answer(f"Ошибка при размуте: {e}")


# -------------------------------------------------------------------
# 6. Управление базой данных
# -------------------------------------------------------------------
@dp.message(Command("personal"))
async def cmd_personal(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.answer("❌ Нет прав.")
    if not message.reply_to_message or not command.args:
        return await message.answer("⚠️ Ответьте на сообщение и укажите команду: `/personal /test`",
                                    parse_mode="Markdown")

    cmd_name = command.args.strip().lstrip('/').split()[0].lower()
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO commands (chat_id, command_name, message_id) VALUES ($1, $2, $3)
                ON CONFLICT (chat_id, command_name) DO UPDATE SET message_id = EXCLUDED.message_id
            """, message.chat.id, cmd_name, message.reply_to_message.message_id)
        await message.answer(f"✅ Команда `/{cmd_name}` сохранена!", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"DB Error (personal): {e}")
        await message.answer("⚠️ Ошибка сохранения в базу данных.")


@dp.message(Command("remove"))
async def cmd_remove(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.answer("❌ Нет прав.")
    if not command.args:
        return await message.answer("⚠️ Укажите команду.")

    cmd_name = command.args.strip().lstrip('/').split()[0].lower()
    try:
        async with db_pool.acquire() as conn:
            res = await conn.execute("DELETE FROM commands WHERE chat_id = $1 AND command_name = $2", message.chat.id,
                                     cmd_name)
            if res == "DELETE 1":
                await message.answer(f"🗑 Команда `/{cmd_name}` удалена.", parse_mode="Markdown")
            else:
                await message.answer(f"⚠️ Команда `/{cmd_name}` не найдена.", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"DB Error (remove): {e}")
        await message.answer("⚠️ Ошибка удаления из базы данных.")


# -------------------------------------------------------------------
# 7. Обработка КАСТОМНЫХ команд (САМАЯ ПОСЛЕДНЯЯ ФУНКЦИЯ!)
# -------------------------------------------------------------------
@dp.message(F.text.startswith('/'))
async def process_custom_command(message: types.Message):
    # Достаем имя команды без / и юзернейма
    raw_cmd = message.text.split()[0].lstrip('/')
    cmd_name = raw_cmd.split('@')[0].lower()

    # Игнорируем стандартные команды, чтобы не было конфликтов
    built_in = {"bot", "ban", "unban", "mute", "unmute", "personal", "remove", "start", "help"}
    if cmd_name in built_in or not cmd_name:
        return

    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT message_id FROM commands WHERE chat_id = $1 AND command_name = $2",
                message.chat.id, cmd_name
            )

            if row:
                try:
                    await bot.copy_message(
                        chat_id=message.chat.id,
                        from_chat_id=message.chat.id,
                        message_id=row['message_id']
                    )
                except Exception as e:
                    logging.error(f"Copy message error: {e}")
                    await message.answer(
                        f"⚠️ Сообщение для команды `/{cmd_name}` удалено. Создайте его заново через /personal.",
                        parse_mode="Markdown")
    except Exception as e:
        logging.error(f"DB Error (custom cmd): {e}")


# -------------------------------------------------------------------
# 8. Запуск приложения
# -------------------------------------------------------------------
async def main():
    logging.basicConfig(level=logging.INFO)

    # Запускаем микро-сервер, чтобы Render дал зеленый свет
    asyncio.create_task(run_health_check_server())

    # Подключаем Neon
    await init_db()

    # Запускаем бота
    logging.info("🚀 Бот запущен и слушает Telegram...")

    # Эта строчка удалит старые ошибки из Telegram, чтобы бот запустился «чистым»
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")