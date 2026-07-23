import asyncio
import logging
import os
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions

# Хранилище команд. Формат: {chat_id: {"bot": message_id}}
custom_commands = {}
TOKEN = os.getenv("BOT_TOKEN", "8978383693:AAGjSvd0D3Culd7GRaSFbVAT-ghdWExHCWk")
DATABASE_URL = os.getenv("DATABASE_URL", "Тpostgresql://postgres:[yplr8NAQkFuskMnz]@db.lpvmpqdyqmnusxsxessf.supabase.co:5432/postgres")
bot = Bot(token=TOKEN)
dp = Dispatcher()
db_pool = None


async def init_db():
    global db_pool
    # Создаем пул соединений с базой данных
    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as conn:
        # BIGINT нужен, так как ID чатов в Telegram очень большие
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS commands (
                chat_id BIGINT,
                command_name TEXT,
                message_id BIGINT,
                PRIMARY KEY (chat_id, command_name)
            )
        """)
async def is_admin(chat_id: int, user_id: int) -> bool:
    """Проверка, является ли пользователь администратором или владельцем чата."""
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in ("administrator", "creator")


def get_target_user(message: types.Message):
    """Вспомогательная функция для получения пользователя из ответа на сообщение."""
    if not message.reply_to_message:
        return None
    return message.reply_to_message.from_user




# -------------------------------------------------------------------
# Блокировка / Разблокировка
# -------------------------------------------------------------------
@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ У вас нет прав для использования этой команды.")

    target = get_target_user(message)
    if not target:
        return await message.reply("⚠️ Ответьте этой командой на сообщение пользователя, которого нужно забанить.")

    try:
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=target.id)
        await message.reply(f"⛔ Пользователь **{target.full_name}** забанен.", parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"Не удалось забанить пользователя. Ошибка: {e}")


@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ У вас нет прав для использования этой команды.")

    target = get_target_user(message)
    if not target:
        return await message.reply("⚠️ Ответьте этой командой на сообщение пользователя, которого нужно разбанить.")

    try:
        # only_if_banned=True гарантирует, что мы снимаем именно бан
        await bot.unban_chat_member(chat_id=message.chat.id, user_id=target.id, only_if_banned=True)
        await message.reply(f"✅ Пользователь **{target.full_name}** разбанен.", parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"Не удалось разбанить пользователя. Ошибка: {e}")


# -------------------------------------------------------------------
# Мут / Размут
# -------------------------------------------------------------------
@dp.message(Command("mute"))
async def cmd_mute(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ У вас нет прав для использования этой команды.")

    target = get_target_user(message)
    if not target:
        return await message.reply("⚠️ Ответьте этой командой на сообщение пользователя, которого нужно заглушить.")

    # Отключаем возможность отправлять сообщения
    no_media_permissions = ChatPermissions(can_send_messages=False)

    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=no_media_permissions
        )
        await message.reply(f"🔇 Пользователь **{target.full_name}** заглушен.", parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"Не удалось заглушить пользователя. Ошибка: {e}")


@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ У вас нет прав для использования этой команды.")

    target = get_target_user(message)
    if not target:
        return await message.reply("⚠️ Ответьте этой командой на сообщение пользователя, с которого нужно снять мут.")

    # Возвращаем стандартные разрешения на отправку сообщений и медиа
    full_permissions = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True
    )

    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=full_permissions
        )
        await message.reply(f"🔊 Пользователю **{target.full_name}** снова разрешено писать в чат.", parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"Не удалось снять ограничения. Ошибка: {e}")


@dp.message(Command("personal"))
async def cmd_personal(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ Нет прав.")
    if not message.reply_to_message:
        return await message.reply("⚠️ Ответьте на сообщение.")
    if not command.args:
        return await message.reply("⚠️ Укажите команду. Пример: `/personal /bot`")

    cmd_name = command.args.strip().lstrip('/').split()[0].lower()
    chat_id = message.chat.id
    msg_id = message.reply_to_message.message_id

    async with db_pool.acquire() as conn:
        # Логика PostgreSQL: если конфликт (такая команда уже есть), то обновляем msg_id
        await conn.execute("""
            INSERT INTO commands (chat_id, command_name, message_id) 
            VALUES ($1, $2, $3)
            ON CONFLICT (chat_id, command_name) 
            DO UPDATE SET message_id = EXCLUDED.message_id
        """, chat_id, cmd_name, msg_id)

    await message.reply(f"✅ Команда `/{cmd_name}` сохранена в базу!", parse_mode="Markdown")


@dp.message(Command("remove"))
async def cmd_remove(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ Нет прав.")
    if not command.args:
        return await message.reply("⚠️ Укажите команду.")

    cmd_name = command.args.strip().lstrip('/').split()[0].lower()
    chat_id = message.chat.id

    async with db_pool.acquire() as conn:
        result = await conn.execute("DELETE FROM commands WHERE chat_id = $1 AND command_name = $2", chat_id, cmd_name)

        # result вернет строку вида "DELETE 1", если запись удалена
        if result == "DELETE 1":
            await message.reply(f"🗑 Команда `/{cmd_name}` удалена из базы.", parse_mode="Markdown")
        else:
            await message.reply(f"⚠️ Команда `/{cmd_name}` не найдена.", parse_mode="Markdown")


@dp.message(F.text.startswith('/'))
async def process_custom_command(message: types.Message):
    cmd_name = message.text.split()[0].lstrip('/').split('@')[0].lower()
    chat_id = message.chat.id

    async with db_pool.acquire() as conn:
        # fetchrow возвращает одну строку
        row = await conn.fetchrow("SELECT message_id FROM commands WHERE chat_id = $1 AND command_name = $2", chat_id,
                                  cmd_name)

        if row:
            msg_id = row['message_id']
            try:
                await bot.copy_message(chat_id=chat_id, from_chat_id=chat_id, message_id=msg_id)
            except Exception:
                await message.reply("⚠️ Исходное сообщение было удалено.")


# -------------------------------------------------------------------
# Точка входа
# -------------------------------------------------------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()  # Подключаемся к базе при старте
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())