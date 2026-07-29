import asyncio
import logging
import os
import time
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import Router
from aiogram.types import Message

# -------------------------------------------------------------------
# 1. Настройки и Переменные
# -------------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 8080))

bot = Bot(token=TOKEN)
dp = Dispatcher()
db_pool = None

# Словари для анти-спама и капчи
spam_tracker = {}
captcha_tasks = {}

# -------------------------------------------------------------------
# 2. Веб-сервер (Health Check для Render)
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id BIGINT PRIMARY KEY,
                captcha_enabled BOOLEAN DEFAULT TRUE,
                welcome_enabled BOOLEAN DEFAULT TRUE,
                antispam_enabled BOOLEAN DEFAULT TRUE,
                del_system_msgs BOOLEAN DEFAULT FALSE
            )
        """)
    logging.info("🗄 База данных успешно подключена!")

async def get_chat_settings(chat_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM chat_settings WHERE chat_id = $1", chat_id)
        if not row:
            await conn.execute("INSERT INTO chat_settings (chat_id) VALUES ($1) ON CONFLICT DO NOTHING", chat_id)
            row = await conn.fetchrow("SELECT * FROM chat_settings WHERE chat_id = $1", chat_id)
        return row

# -------------------------------------------------------------------
# 4. Вспомогательные функции
# -------------------------------------------------------------------
async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except:
        return False

def get_target_user(message: types.Message):
    if not message.reply_to_message:
        return None
    return message.reply_to_message.from_user

# -------------------------------------------------------------------
# 5. Middleware (Анти-спам фильтр)
# -------------------------------------------------------------------
class AntiSpamMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.Message, data: dict):
        if not event.text and not event.photo and not event.video:
            return await handler(event, data)

        if event.chat.type in ("private", "channel") or db_pool is None:
            return await handler(event, data)

        settings = await get_chat_settings(event.chat.id)
        if not settings['antispam_enabled']:
            return await handler(event, data)

        user_id = event.from_user.id
        chat_id = event.chat.id
        msg_id = event.message_id
        now = time.time()

        if user_id not in spam_tracker:
            spam_tracker[user_id] = []

        # Изменил окно до 4 секунд, чтобы ловило реальный флуд, а не лаги телеги
        spam_tracker[user_id] = [(t, m) for t, m in spam_tracker[user_id] if now - t <= 4.0]
        spam_tracker[user_id].append((now, msg_id))

        if len(spam_tracker[user_id]) > 5:
            if not await is_admin(chat_id, user_id):
                until_date = int(now) + 3 * 24 * 3600
                perms = ChatPermissions(can_send_messages=False)
                try:
                    await bot.restrict_chat_member(chat_id, user_id, permissions=perms, until_date=until_date)
                    for _, m_id in spam_tracker[user_id]:
                        try:
                            await bot.delete_message(chat_id, m_id)
                        except:
                            pass

                    spam_tracker[user_id].clear()
                    await bot.send_message(chat_id, f"🛑 Пользователь {event.from_user.full_name} заглушен на 3 дня за флуд/спам.")
                except Exception as e:
                    logging.error(f"AntiSpam Error: {e}")
                return

        return await handler(event, data)

# -------------------------------------------------------------------
# 6. Настройки чата (Меню /settings)
# -------------------------------------------------------------------
def get_settings_keyboard(settings):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🧠 Капча: {'✅' if settings['captcha_enabled'] else '❌'}", callback_data="set_captcha")],
        [InlineKeyboardButton(text=f"💬 Приветствие: {'✅' if settings['welcome_enabled'] else '❌'}", callback_data="set_welcome")],
        [InlineKeyboardButton(text=f"🛡 Анти-спам: {'✅' if settings['antispam_enabled'] else '❌'}", callback_data="set_antispam")],
        [InlineKeyboardButton(text=f"🗑 Удалять входы/выходы: {'✅' if settings['del_system_msgs'] else '❌'}", callback_data="set_delsys")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="set_close")]
    ])
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Привет! Бот работает.")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer("Вот список доступных команд:\n/start\n/help\n/settings\n/ban\n/unban\n/mute\n/unmute\n/personal\n/remove")

@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    if message.chat.type == "private":
        return await message.answer("Эта команда работает только в группах.")
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.answer("❌ Нет прав.")

    settings = await get_chat_settings(message.chat.id)
    kb = get_settings_keyboard(settings)
    await message.answer("⚙️ **Параметры группы:**\nВыберите функцию для переключения:", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("set_"))
async def process_settings_callback(call: types.CallbackQuery):
    if not await is_admin(call.message.chat.id, call.from_user.id):
        return await call.answer("❌ Нет прав для изменения настроек.", show_alert=True)

    action = call.data.split("_")[1]

    if action == "close":
        try:
            await call.message.delete()
        except:
            pass
        return await call.answer()

    col_map = {
        "captcha": "captcha_enabled",
        "welcome": "welcome_enabled",
        "antispam": "antispam_enabled",
        "delsys": "del_system_msgs"
    }

    col = col_map.get(action)
    if col:
        async with db_pool.acquire() as conn:
            await conn.execute(f"UPDATE chat_settings SET {col} = NOT {col} WHERE chat_id = $1", call.message.chat.id)

        settings = await get_chat_settings(call.message.chat.id)
        kb = get_settings_keyboard(settings)
        try:
            await call.message.edit_reply_markup(reply_markup=kb)
        except:
            pass

    await call.answer("Настройка обновлена!")

# -------------------------------------------------------------------
# 7. Системные команды модерации
# -------------------------------------------------------------------
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
# 8. Управление базой данных (Кастомные команды)
# -------------------------------------------------------------------
@dp.message(Command("personal"))
async def cmd_personal(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.answer("❌ Нет прав.")
    if not message.reply_to_message or not command.args:
        return await message.answer("⚠️ Ответьте на сообщение и укажите команду: `/personal /test`", parse_mode="Markdown")

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
        await message.answer("⚠️ Ошибка сохранения в БД.")

@dp.message(Command("remove"))
async def cmd_remove(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.answer("❌ Нет прав.")
    if not command.args:
        return await message.answer("⚠️ Укажите команду.")

    cmd_name = command.args.strip().lstrip('/').split()[0].lower()
    try:
        async with db_pool.acquire() as conn:
            res = await conn.execute("DELETE FROM commands WHERE chat_id = $1 AND command_name = $2", message.chat.id, cmd_name)
            if res == "DELETE 1":
                await message.answer(f"🗑 Команда `/{cmd_name}` удалена.", parse_mode="Markdown")
            else:
                await message.answer(f"⚠️ Команда `/{cmd_name}` не найдена.", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"DB Error (remove): {e}")

# -------------------------------------------------------------------
# 9. Защита от ботов (Капча) и Приветствие
# -------------------------------------------------------------------
async def kick_if_not_passed(chat_id, user_id, captcha_msg_id):
    await asyncio.sleep(120)
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
        await bot.delete_message(chat_id, captcha_msg_id)
    except:
        pass

async def send_welcome_message(chat_id, user):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Наш сайт", url="https://marmelad.cc/shop/")]
    ])
    await bot.send_message(
        chat_id,
        f"🎉 {user.full_name}, добро пожаловать к нам!\nРады тебя видеть. Жми на кнопку ниже 👇",
        reply_markup=kb
    )

@dp.message(F.left_chat_member)
async def on_user_leave(message: types.Message):
    settings = await get_chat_settings(message.chat.id)
    if settings['del_system_msgs']:
        try:
            await message.delete()
        except:
            pass

@dp.message(F.new_chat_members)
async def on_user_join(message: types.Message):
    settings = await get_chat_settings(message.chat.id)

    if settings['del_system_msgs']:
        try:
            await message.delete()
        except:
            pass

    for new_user in message.new_chat_members:
        if new_user.is_bot:
            continue

        if settings['captcha_enabled']:
            perms = ChatPermissions(can_send_messages=False)
            try:
                await bot.restrict_chat_member(message.chat.id, new_user.id, permissions=perms)
            except Exception as e:
                logging.error(f"Captcha restrict error: {e}")
                await message.answer(f"⚠️ Ошибка: не могу выдать мут {new_user.full_name}! Дайте боту право **Блокировать пользователей**.")
                # Если бот не может выдать мут, шлем просто приветствие (если оно включено) и пропускаем капчу
                if settings['welcome_enabled']:
                    await send_welcome_message(message.chat.id, new_user)
                continue

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🤖 Я не робот", callback_data=f"captcha_{new_user.id}")]
            ])

            captcha_msg = await message.answer(
                f"Привет, {new_user.full_name}! 👋\nНажми кнопку ниже в течение 2 минут, чтобы доказать, что ты не бот.",
                reply_markup=kb
            )

            task = asyncio.create_task(kick_if_not_passed(message.chat.id, new_user.id, captcha_msg.message_id))
            captcha_tasks[f"{message.chat.id}_{new_user.id}"] = task
        else:
            if settings['welcome_enabled']:
                await send_welcome_message(message.chat.id, new_user)

@dp.callback_query(F.data.startswith("captcha_"))
async def process_captcha(call: types.CallbackQuery):
    target_user_id = int(call.data.split("_")[1])

    if call.from_user.id != target_user_id:
        return await call.answer("Это не твоя кнопка! 👀", show_alert=True)

    task_key = f"{call.message.chat.id}_{target_user_id}"
    if task_key in captcha_tasks:
        captcha_tasks[task_key].cancel()
        del captcha_tasks[task_key]

    perms = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True)
    try:
        await bot.restrict_chat_member(call.message.chat.id, target_user_id, permissions=perms)
    except Exception as e:
        logging.error(f"Captcha unrestrict error: {e}")

    try:
        await call.message.delete()
    except:
        pass

    settings = await get_chat_settings(call.message.chat.id)
    if settings['welcome_enabled']:
        await send_welcome_message(call.message.chat.id, call.from_user)

# -------------------------------------------------------------------
# 10. Обработка КАСТОМНЫХ команд
# -------------------------------------------------------------------
@dp.message(F.text.startswith('/'))
async def process_custom_command(message: types.Message):
    raw_cmd = message.text.split()[0].lstrip('/')
    cmd_name = raw_cmd.split('@')[0].lower()

    built_in = {"ban", "unban", "mute", "unmute", "personal", "remove", "start", "help", "settings"}
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
                    await bot.copy_message(chat_id=message.chat.id, from_chat_id=message.chat.id,
                                           message_id=row['message_id'])
                except Exception as e:
                    logging.error(f"Copy message error: {e}")
    except Exception as e:
        logging.error(f"DB Error (custom cmd): {e}")

# -------------------------------------------------------------------
# 11. Запуск
# -------------------------------------------------------------------
async def main():
    logging.basicConfig(level=logging.INFO)

    dp.message.middleware(AntiSpamMiddleware())

    asyncio.create_task(run_health_check_server())
    await init_db()

    logging.info("🚀 Бот запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")