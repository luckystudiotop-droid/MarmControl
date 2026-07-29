import asyncio
import logging
import os
import re
import time
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton, Message

# -------------------------------------------------------------------
# 1. Настройки и Переменные
# -------------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 8080))

bot = Bot(token=TOKEN)
dp = Dispatcher()
db_pool = None

# Единый источник правды для системных команд — используется и при создании
# кастомных команд (/personal), и при их вызове, чтобы не разойтись
BUILT_IN_COMMANDS = {"ban", "unban", "mute", "unmute", "personal", "remove", "start", "help", "settings"}

# Служебные типы сообщений — их не нужно учитывать в анти-спаме
SERVICE_CONTENT_TYPES = {
    "new_chat_members", "left_chat_member", "pinned_message",
    "new_chat_title", "new_chat_photo", "delete_chat_photo",
    "group_chat_created", "supergroup_chat_created", "channel_chat_created",
}

# Словари для анти-спама и капчи
# ИСПРАВЛЕНО: ключ (chat_id, user_id) вместо просто user_id — иначе активность
# юзера в разных чатах, где стоит бот, смешивалась в один счётчик
spam_tracker = {}
captcha_tasks = {}
punishing_users = set()  # (chat_id, user_id), которые прямо сейчас наказываются — защита от гонки

# Кэш настроек чата, чтобы не ходить в БД на каждое сообщение
settings_cache = {}  # chat_id -> (row, timestamp)
SETTINGS_CACHE_TTL = 30  # секунд

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
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=30,
        # ИСПРАВЛЕНО: обязательно для Neon, если соединение идёт через пулер
        # (PgBouncer, transaction mode) — иначе со временем ловите ошибку
        # "prepared statement already exists"
        statement_cache_size=0,
    )
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

async def get_chat_settings(chat_id: int, force_refresh: bool = False):
    # ИСПРАВЛЕНО: локальный кэш с TTL — раньше каждое сообщение в группе
    # дёргало БД, теперь только раз в SETTINGS_CACHE_TTL секунд или сразу
    # после изменения настроек (force_refresh)
    now = time.time()
    if not force_refresh:
        cached = settings_cache.get(chat_id)
        if cached and now - cached[1] < SETTINGS_CACHE_TTL:
            return cached[0]

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM chat_settings WHERE chat_id = $1", chat_id)
        if not row:
            await conn.execute("INSERT INTO chat_settings (chat_id) VALUES ($1) ON CONFLICT DO NOTHING", chat_id)
            row = await conn.fetchrow("SELECT * FROM chat_settings WHERE chat_id = $1", chat_id)

    settings_cache[chat_id] = (row, now)
    return row

def invalidate_settings_cache(chat_id: int):
    settings_cache.pop(chat_id, None)

# -------------------------------------------------------------------
# 4. Вспомогательные функции
# -------------------------------------------------------------------
async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
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
        # ИСПРАВЛЕНО: раньше учитывались только text/photo/video — стикеры,
        # войсы, документы и GIF полностью обходили анти-спам. Теперь
        # исключаем только служебные сообщения (join/leave/pin и т.п.)
        if event.content_type in SERVICE_CONTENT_TYPES:
            return await handler(event, data)

        if event.chat.type in ("private", "channel") or db_pool is None:
            return await handler(event, data)

        settings = await get_chat_settings(event.chat.id)
        if not settings["antispam_enabled"]:
            return await handler(event, data)

        user_id = event.from_user.id
        chat_id = event.chat.id
        msg_id = event.message_id
        key = (chat_id, user_id)
        now = time.time()

        spam_tracker.setdefault(key, [])
        # Окно 4 секунды, чтобы ловить реальный флуд, а не лаги телеги
        spam_tracker[key] = [(t, m) for t, m in spam_tracker[key] if now - t <= 4.0]
        spam_tracker[key].append((now, msg_id))

        if len(spam_tracker[key]) > 5:
            # ИСПРАВЛЕНО: защита от гонки — если несколько сообщений одного
            # юзера обрабатываются параллельно, наказываем только один раз
            if key in punishing_users:
                return
            punishing_users.add(key)
            try:
                if not await is_admin(chat_id, user_id):
                    until_date = int(now) + 3 * 24 * 3600
                    perms = ChatPermissions(can_send_messages=False)
                    try:
                        await bot.restrict_chat_member(chat_id, user_id, permissions=perms, until_date=until_date)
                        for _, m_id in spam_tracker[key]:
                            try:
                                await bot.delete_message(chat_id, m_id)
                            except Exception:
                                pass

                        spam_tracker[key].clear()
                        await bot.send_message(
                            chat_id,
                            f"🛑 Пользователь {event.from_user.full_name} заглушен на 3 дня за флуд/спам."
                        )
                    except Exception as e:
                        logging.error(f"AntiSpam Error: {e}")
            finally:
                punishing_users.discard(key)
            return

        return await handler(event, data)

async def cleanup_spam_tracker():
    """Фоновая чистка старых записей — иначе spam_tracker растёт бесконечно
    для юзеров, которые прислали пару сообщений и больше не писали."""
    while True:
        await asyncio.sleep(600)
        now = time.time()
        for key in list(spam_tracker.keys()):
            spam_tracker[key] = [(t, m) for t, m in spam_tracker[key] if now - t <= 4.0]
            if not spam_tracker[key]:
                del spam_tracker[key]

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
    # ИСПРАВЛЕНО: **text** в legacy Markdown не даёт жирный текст (нужен один *)
    await message.answer("⚙️ *Параметры группы:*\nВыберите функцию для переключения:", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("set_"))
async def process_settings_callback(call: types.CallbackQuery):
    if not await is_admin(call.message.chat.id, call.from_user.id):
        return await call.answer("❌ Нет прав для изменения настроек.", show_alert=True)

    action = call.data.split("_")[1]

    if action == "close":
        try:
            await call.message.delete()
        except Exception:
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
        # col берётся из фиксированного словаря выше, а не из пользовательского
        # ввода — f-string здесь безопасен, инъекция невозможна
        async with db_pool.acquire() as conn:
            await conn.execute(f"UPDATE chat_settings SET {col} = NOT {col} WHERE chat_id = $1", call.message.chat.id)

        # ИСПРАВЛЕНО: сбрасываем кэш и перечитываем настройки сразу же,
        # иначе клавиатура могла бы до SETTINGS_CACHE_TTL секунд показывать старое состояние
        invalidate_settings_cache(call.message.chat.id)
        settings = await get_chat_settings(call.message.chat.id, force_refresh=True)
        kb = get_settings_keyboard(settings)
        try:
            await call.message.edit_reply_markup(reply_markup=kb)
        except Exception:
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
    # ИСПРАВЛЕНО: явная защита администраторов от бана другим админом
    if await is_admin(message.chat.id, target.id):
        return await message.answer("❌ Нельзя забанить администратора чата.")
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
    # ИСПРАВЛЕНО: явная защита администраторов от мута другим админом
    if await is_admin(message.chat.id, target.id):
        return await message.answer("❌ Нельзя замьютить администратора чата.")
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
        return await message.answer("⚠️ Ответьте на сообщение и укажите команду: /personal test")

    cmd_name = command.args.strip().lstrip('/').split()[0].lower()

    # ИСПРАВЛЕНО: раньше системное имя (ban, start, settings...) молча
    # сохранялось в БД, но никогда не срабатывало — админ не получал об
    # этом никакого предупреждения. Теперь отклоняем сразу.
    if cmd_name in BUILT_IN_COMMANDS:
        return await message.answer(f"⚠️ Имя «{cmd_name}» зарезервировано системной командой, выберите другое.")

    if not re.fullmatch(r"[a-zа-яё0-9_]{1,32}", cmd_name):
        return await message.answer("⚠️ Название команды может содержать только буквы, цифры и «_» (до 32 символов).")

    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO commands (chat_id, command_name, message_id) VALUES ($1, $2, $3)
                ON CONFLICT (chat_id, command_name) DO UPDATE SET message_id = EXCLUDED.message_id
            """, message.chat.id, cmd_name, message.reply_to_message.message_id)
        # ИСПРАВЛЕНО: убрал parse_mode="Markdown" с подстановкой пользовательских
        # данных — спецсимволы в имени команды могли ломать парсинг разметки
        await message.answer(f"✅ Команда «/{cmd_name}» сохранена!")
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
                await message.answer(f"🗑 Команда «/{cmd_name}» удалена.")
            else:
                await message.answer(f"⚠️ Команда «/{cmd_name}» не найдена.")
    except Exception as e:
        logging.error(f"DB Error (remove): {e}")
        await message.answer("⚠️ Ошибка удаления из БД.")

# -------------------------------------------------------------------
# 9. Защита от ботов (Капча) и Приветствие
# -------------------------------------------------------------------
async def kick_if_not_passed(chat_id, user_id, captcha_msg_id):
    await asyncio.sleep(120)
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
        await bot.delete_message(chat_id, captcha_msg_id)
    except Exception as e:
        logging.error(f"Captcha kick error: {e}")
    finally:
        # ИСПРАВЛЕНО: раньше запись из captcha_tasks удалялась только при
        # нажатии кнопки — при кике по таймауту оставалась висеть в памяти навсегда
        captcha_tasks.pop(f"{chat_id}_{user_id}", None)

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
        except Exception:
            pass

@dp.message(F.new_chat_members)
async def on_user_join(message: types.Message):
    settings = await get_chat_settings(message.chat.id)

    if settings['del_system_msgs']:
        try:
            await message.delete()
        except Exception:
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
    except Exception:
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

    if cmd_name in BUILT_IN_COMMANDS or not cmd_name:
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
async def on_shutdown():
    logging.info("🛑 Останавливаюсь, закрываю пул соединений с БД...")
    if db_pool:
        await db_pool.close()

async def main():
    logging.basicConfig(level=logging.INFO)

    dp.message.middleware(AntiSpamMiddleware())
    dp.shutdown.register(on_shutdown)

    asyncio.create_task(run_health_check_server())
    asyncio.create_task(cleanup_spam_tracker())
    await init_db()

    logging.info("🚀 Бот запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")