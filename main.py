import asyncio
import logging
import os
import time
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton

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
# 5. Middleware (Анти-спам фильтр)
# -------------------------------------------------------------------
class AntiSpamMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.Message, data: dict):
        # Проверяем только текстовые и медиа сообщения
        if not event.text and not event.photo and not event.video:
            return await handler(event, data)

        user_id = event.from_user.id
        chat_id = event.chat.id
        msg_id = event.message_id
        now = time.time()

        if user_id not in spam_tracker:
            spam_tracker[user_id] = []

        # Оставляем в памяти только сообщения за последнюю 1 секунду
        spam_tracker[user_id] = [(t, m) for t, m in spam_tracker[user_id] if now - t <= 1.0]
        spam_tracker[user_id].append((now, msg_id))

        # Если больше 5 сообщений за секунду - НАКАЗАНИЕ
        if len(spam_tracker[user_id]) > 5:
            if not await is_admin(chat_id, user_id):
                # Мут на 3 дня
                until_date = int(now) + 3 * 24 * 3600
                perms = ChatPermissions(can_send_messages=False)
                try:
                    await bot.restrict_chat_member(chat_id, user_id, permissions=perms, until_date=until_date)

                    # Удаляем спам-сообщения
                    for _, m_id in spam_tracker[user_id]:
                        try:
                            await bot.delete_message(chat_id, m_id)
                        except:
                            pass

                    spam_tracker[user_id].clear()
                    await bot.send_message(chat_id,
                                           f"🛑 Пользователь {event.from_user.full_name} заглушен на 3 дня за флуд/спам.")
                except Exception as e:
                    logging.error(f"AntiSpam Error: {e}")
                return  # Прерываем обработку спама

        return await handler(event, data)


# -------------------------------------------------------------------
# 6. Системные команды модерации
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
# 7. Управление базой данных (Кастомные команды)
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
            res = await conn.execute("DELETE FROM commands WHERE chat_id = $1 AND command_name = $2", message.chat.id,
                                     cmd_name)
            if res == "DELETE 1":
                await message.answer(f"🗑 Команда `/{cmd_name}` удалена.", parse_mode="Markdown")
            else:
                await message.answer(f"⚠️ Команда `/{cmd_name}` не найдена.", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"DB Error (remove): {e}")


# -------------------------------------------------------------------
# 8. Защита от ботов (Капча) и Приветствие
# -------------------------------------------------------------------
async def kick_if_not_passed(chat_id, user_id, captcha_msg_id):
    await asyncio.sleep(120)  # Ждем 2 минуты
    try:
        # Если задача не отменена (юзер не нажал кнопку) - кикаем
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
        await bot.delete_message(chat_id, captcha_msg_id)
    except:
        pass


async def delete_msg_later(chat_id, msg_id, delay):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, msg_id)
    except:
        pass


@dp.message(F.new_chat_members)
async def on_user_join(message: types.Message):
    # УДАЛИЛИ строчку message.delete(), чтобы история чата у нового юзера не пропадала!

    for new_user in message.new_chat_members:
        # Если добавили бота - пропускаем
        if new_user.is_bot:
            continue

        # 1. Бросаем в мут
        perms = ChatPermissions(can_send_messages=False)
        try:
            await bot.restrict_chat_member(message.chat.id, new_user.id, permissions=perms)
        except:
            continue

        # 2. Отправляем капчу
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Я не робот", callback_data=f"captcha_{new_user.id}")]
        ])

        captcha_msg = await message.answer(
            f"Привет, {new_user.full_name}! 👋\nНажми кнопку ниже в течение 2 минут, чтобы доказать, что ты не бот.",
            reply_markup=kb
        )

        # 3. Запускаем таймер на кик
        task = asyncio.create_task(kick_if_not_passed(message.chat.id, new_user.id, captcha_msg.message_id))
        captcha_tasks[f"{message.chat.id}_{new_user.id}"] = task


@dp.callback_query(F.data.startswith("captcha_"))
async def process_captcha(call: types.CallbackQuery):
    target_user_id = int(call.data.split("_")[1])

    # Защита: только тот юзер может нажать кнопку
    if call.from_user.id != target_user_id:
        return await call.answer("Это не твоя кнопка! 👀", show_alert=True)

    # Отменяем кик
    task_key = f"{call.message.chat.id}_{target_user_id}"
    if task_key in captcha_tasks:
        captcha_tasks[task_key].cancel()
        del captcha_tasks[task_key]

    # Снимаем мут
    perms = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True)
    await bot.restrict_chat_member(call.message.chat.id, target_user_id, permissions=perms)

    # Удаляем сообщение с капчей
    try:
        await call.message.delete()
    except:
        pass

    # ОТПРАВЛЯЕМ ПРИВЕТСТВИЕ С КНОПКОЙ
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Наш сайт", url="https://marmelad.cc/shop/")]
        # <-- ЗАМЕНИТЬ ССЫЛКУ ТУТ
    ])

    welcome_msg = await bot.send_message(
        call.message.chat.id,
        f"🎉 {call.from_user.full_name}, добро пожаловать к нам!\nРады тебя видеть. Жми на кнопку ниже 👇",
        reply_markup=kb
    )

    # Удаляем приветствие через 2 минуты (120 секунд)



# -------------------------------------------------------------------
# 9. Обработка КАСТОМНЫХ команд
# -------------------------------------------------------------------
@dp.message(F.text.startswith('/'))
async def process_custom_command(message: types.Message):
    raw_cmd = message.text.split()[0].lstrip('/')
    cmd_name = raw_cmd.split('@')[0].lower()

    built_in = {"ban", "unban", "mute", "unmute", "personal", "remove", "start", "help"}
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
# 10. Запуск
# -------------------------------------------------------------------
async def main():
    logging.basicConfig(level=logging.INFO)

    # Регистрируем Анти-спам (Middleware)
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