import asyncio
import sqlite3
import html
import time
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram import F
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import UserStatusOnline
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
    AuthKeyUnregisteredError,
    UserDeactivatedBanError,
    UnauthorizedError
)

DB_NAME = 'user_sessions.db'
DEFAULT_REPLY_TEXT = "Hozircha bandman, bo'shashim bilan aloqaga chiqaman."
DEFAULT_DELAY = 7  

# ========== ADMIN VA SOZLAMALAR ==========
ADMIN_USERNAME = "neopulse_uz"
API_ID = 37437082
API_HASH = "b7d4fa4d28472bf3768a4cae5e3fd01c"
BOT_TOKEN = "8995093768:AAFe3bNqPLbaNwynNRNMMhTiXUk_YCi7b9s"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ========== XOTIRA ==========
user_clients = {}         
auto_tasks = {}           
waiting_for = {}           
phone_cache = {}           
phone_code_hash_cache = {} 


# ========== MA'LUMOTLAR BAZASI ==========
def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS sessions
                     (user_id INTEGER PRIMARY KEY, session_string TEXT, custom_text TEXT, delay_seconds INTEGER DEFAULT 7)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS custom_triggers
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, keyword TEXT, response TEXT)''')

        c.execute('''CREATE TABLE IF NOT EXISTS sent_auto_replies
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER, msg_id INTEGER)''')
        
        conn.commit()

def save_session(user_id, session_string):
    with get_db() as conn:
        conn.execute('''INSERT INTO sessions (user_id, session_string) VALUES (?, ?)
                     ON CONFLICT(user_id) DO UPDATE SET session_string=excluded.session_string''', 
                  (user_id, session_string))

def has_active_session(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT session_string FROM sessions WHERE user_id=? AND session_string IS NOT NULL', (user_id,))
        row = c.fetchone()
        return row is not None and bool(row[0])

def set_custom_reply_text(user_id, text):
    with get_db() as conn:
        conn.execute('''INSERT INTO sessions (user_id, custom_text) VALUES (?, ?)
                     ON CONFLICT(user_id) DO UPDATE SET custom_text=excluded.custom_text''', 
                  (user_id, text))

def get_custom_reply_text(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT custom_text FROM sessions WHERE user_id=?', (user_id,))
        row = c.fetchone()
        return row[0] if row and row[0] else DEFAULT_REPLY_TEXT

def set_user_delay(user_id, seconds):
    with get_db() as conn:
        conn.execute('''INSERT INTO sessions (user_id, delay_seconds) VALUES (?, ?)
                     ON CONFLICT(user_id) DO UPDATE SET delay_seconds=excluded.delay_seconds''', 
                  (user_id, seconds))

def get_user_delay(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT delay_seconds FROM sessions WHERE user_id=?', (user_id,))
        row = c.fetchone()
        return row[0] if row and row[0] is not None else DEFAULT_DELAY

def get_all_sessions():
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT user_id, session_string FROM sessions WHERE session_string IS NOT NULL')
        return c.fetchall()

def get_total_users_count():
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM sessions')
        return c.fetchone()[0]

def delete_session(user_id):
    with get_db() as conn:
        conn.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
        conn.execute('DELETE FROM sent_auto_replies WHERE user_id=?', (user_id,))
        conn.execute('DELETE FROM custom_triggers WHERE user_id=?', (user_id,))

def add_custom_trigger(user_id, keyword, response):
    with get_db() as conn:
        conn.execute('INSERT INTO custom_triggers (user_id, keyword, response) VALUES (?, ?, ?)',
                     (user_id, keyword.lower().strip(), response))

def get_matching_response(user_id, message_text):
    if not message_text:
        return None
    clean_msg = message_text.lower().strip()
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT keyword, response FROM custom_triggers WHERE user_id=?', (user_id,))
        triggers = c.fetchall()
        for kw, resp in triggers:
            if kw == clean_msg or kw in clean_msg:
                return resp
    return None

def get_user_triggers(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT id, keyword, response FROM custom_triggers WHERE user_id=?', (user_id,))
        return c.fetchall()

def delete_trigger_by_id(trigger_id, user_id):
    with get_db() as conn:
        conn.execute('DELETE FROM custom_triggers WHERE id=? AND user_id=?', (trigger_id, user_id))

def save_sent_reply(user_id, chat_id, msg_id):
    with get_db() as conn:
        conn.execute('INSERT INTO sent_auto_replies (user_id, chat_id, msg_id) VALUES (?, ?, ?)',
                     (user_id, chat_id, msg_id))

def get_and_delete_all_sent_replies(user_id, chat_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT msg_id FROM sent_auto_replies WHERE user_id=? AND chat_id=?', (user_id, chat_id))
        rows = c.fetchall()
        if rows:
            c.execute('DELETE FROM sent_auto_replies WHERE user_id=? AND chat_id=?', (user_id, chat_id))
            conn.commit()
            return [r[0] for r in rows]
        return []

def is_admin(user: types.User) -> bool:
    if user.username and user.username.lower() == ADMIN_USERNAME.lower():
        return True
    return False


# ========== TELETHON AVTO JAVOB FUNKSIYASI ==========
async def start_auto_reply(user_id, client: TelegramClient):
    @client.on(events.NewMessage(incoming=True))
    async def auto_reply_handler(event):
        if not event.is_private or event.out:
            return

        try:
            sender = await event.get_sender()
            if not sender or getattr(sender, 'bot', False):
                return

            # --- CHEKSIZ SIKL (LOOP) NING OLDINI OLISH FILTRI ---
            my_custom_text = get_custom_reply_text(user_id)
            if event.raw_text and event.raw_text.strip() == my_custom_text.strip():
                return

            all_triggers = get_user_triggers(user_id)
            for _, _, resp_text in all_triggers:
                if event.raw_text and event.raw_text.strip() == resp_text.strip():
                    return
            # --------------------------------------------------

            full_user = await client(GetFullUserRequest(event.sender_id))
            is_online = isinstance(full_user.users[0].status, UserStatusOnline)

            if is_online:
                delay = get_user_delay(user_id)
                await asyncio.sleep(delay)

            matched_resp = get_matching_response(user_id, event.raw_text)
            if matched_resp:
                reply_text = matched_resp
            else:
                reply_text = my_custom_text

            sent_msg = await event.reply(reply_text)
            save_sent_reply(user_id, event.chat_id, sent_msg.id)

        except (AuthKeyUnregisteredError, UserDeactivatedBanError, UnauthorizedError):
            stop_client_task(user_id)
            delete_session(user_id)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"Xatolik auto reply (user={user_id}): {e}")

    @client.on(events.NewMessage(outgoing=True))
    async def outgoing_handler(event):
        if not event.is_private:
            return

        try:
            chat_id = event.chat_id
            msg_ids = get_and_delete_all_sent_replies(user_id, chat_id)
            if msg_ids:
                await client.delete_messages(chat_id, msg_ids)
        except Exception as e:
            print(f"Avto-javoblarni o'chirishda xatolik: {e}")

    @client.on(events.MessageRead(out=False))
    async def message_read_handler(event):
        try:
            chat_id = event.chat_id
            msg_ids = get_and_delete_all_sent_replies(user_id, chat_id)
            if msg_ids:
                await client.delete_messages(chat_id, msg_ids)
        except Exception as e:
            print(f"Read hodisasida avto-javoblarni o'chirishda xatolik: {e}")

    try:
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        pass
    except (AuthKeyUnregisteredError, UserDeactivatedBanError, UnauthorizedError):
        delete_session(user_id)
    except Exception as e:
        print(f"Ulanishda kutilmagan xato (user={user_id}): {e}")
    finally:
        user_clients.pop(user_id, None)
        auto_tasks.pop(user_id, None)


def stop_client_task(user_id):
    client = user_clients.get(user_id)
    task = auto_tasks.get(user_id)
    if client:
        try:
            asyncio.create_task(client.disconnect())
        except Exception:
            pass
    if task and not task.done():
        task.cancel()
    user_clients.pop(user_id, None)
    auto_tasks.pop(user_id, None)


# ========== TUGMALAR VA MENYULAR ==========
def get_main_keyboard(is_connected=False):
    buttons = []
    if is_connected:
        buttons.append([InlineKeyboardButton(text="➕ So'rov/Javob qo'shish (Kalit so'z)", callback_data="add_trigger")])
        buttons.append([InlineKeyboardButton(text="📋 Barcha so'rovlarni ko'rish", callback_data="list_triggers")])
        buttons.append([InlineKeyboardButton(text="✏️ Asosiy javobni o'zgartirish", callback_data="change_text")])
        buttons.append([InlineKeyboardButton(text="⏱ Kutiladigan vaqtni o'zgartirish", callback_data="change_delay")])
        buttons.append([InlineKeyboardButton(text="⏹ Avto-javobni to'xtatish", callback_data="stop_auto")])
    else:
        buttons.append([InlineKeyboardButton(text="🚀 Boshlash (Ulanish)", callback_data="start_auto")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_reply_keyboard(user: types.User):
    keyboard = []
    if is_admin(user):
        keyboard.append([KeyboardButton(text="👑 Admin Panel"), KeyboardButton(text="📊 Statistika")])
        keyboard.append([KeyboardButton(text="📢 Xabar yuborish")])
    
    keyboard.append([KeyboardButton(text="⚙️ Sozlamalar / Bosh menyu")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


# ========== START VA ADMIN BUYRUQLARI ==========
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    is_connected = has_active_session(user_id)
    reply_kb = get_reply_keyboard(message.from_user)

    if is_connected:
        current_text = get_custom_reply_text(user_id)
        current_delay = get_user_delay(user_id)
        safe_text = html.escape(current_text)
        
        await message.answer(
            f"Assalomu alaykum! 👋\n\n"
            f"💬 <b>Asosiy avto-javob matningiz:</b>\n<code>{safe_text}</code>\n\n"
            f"⏱ <b>Online kutiladigan vaqt:</b> <code>{current_delay} soniya</code>\n\n"
            f"Kerakli bo'limni tanlang:",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(True)
        )
        await message.answer("👇 Qulay boshqaruv menyusi:", reply_markup=reply_kb)
    else:
        await message.answer(
            "Assalomu alaykum! 👋\n\n"
            "Botdan foydalanish uchun Telegram akkauntingizni ulang.",
            reply_markup=get_main_keyboard(False)
        )
        await message.answer("👇 Qulay boshqaruv menyusi:", reply_markup=reply_kb)


@dp.message(F.text == "👑 Admin Panel")
async def admin_panel_cmd(message: types.Message):
    if not is_admin(message.from_user):
        return
    await message.answer(
        "🛠 <b>Admin Panel</b>\n\n"
        "Siz bot tizimida admin huquqiga egasiz.\n"
        "Quyidagi tugmalar orqali botni boshqarishingiz mumkin:",
        parse_mode="HTML"
    )

@dp.message(F.text == "📊 Statistika")
async def admin_stats_cmd(message: types.Message):
    if not is_admin(message.from_user):
        return
    total_users = get_total_users_count()
    active_sessions = len(get_all_sessions())
    
    await message.answer(
        f"📊 <b>Bot Statistikasi:</b>\n\n"
        f"👥 Barcha foydalanuvchilar: <b>{total_users}</b>\n"
        f"⚡️ Faol ulangan seanslar: <b>{active_sessions}</b>",
        parse_mode="HTML"
    )

@dp.message(F.text == "📢 Xabar yuborish")
async def admin_broadcast_cmd(message: types.Message):
    if not is_admin(message.from_user):
        return
    waiting_for[message.from_user.id] = "admin_broadcast"
    await message.answer("📝 Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yozing:")

@dp.message(F.text == "⚙️ Sozlamalar / Bosh menyu")
async def settings_cmd(message: types.Message):
    user_id = message.from_user.id
    is_connected = has_active_session(user_id)
    await message.answer("⚙️ <b>Bosh menyu va sozlamalar:</b>", parse_mode="HTML", reply_markup=get_main_keyboard(is_connected))


# ========== CALLBACK VA MATN ISHLOVCHILARI ==========
@dp.callback_query(F.data == "add_trigger")
async def add_trigger_cb(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    waiting_for[user_id] = "trigger_keyword"
    await callback.message.answer(
        "🔹 **Qaysi kichik gap/so'z yozilganda javob berilsin?**\n\n"
        "Masalan: `salom` yoki `qayerdasiz`",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "list_triggers")
async def list_triggers_cb(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    triggers = get_user_triggers(user_id)
    
    if not triggers:
        await callback.message.answer("📭 Siz hali hech qanday maxsus so'rov qo'shmadingiz.")
        await callback.answer()
        return

    msg = "📋 **Siz qo'shgan maxsus so'rovlar:**\n\n"
    buttons = []
    for t_id, kw, resp in triggers:
        msg += f"🔸 **So'rov:** `{kw}` ➡️ **Javob:** `{resp}`\n"
        buttons.append([InlineKeyboardButton(text=f"❌ O'chirish: {kw}", callback_data=f"del_trg_{t_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer(msg, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("del_trg_"))
async def delete_trigger_cb(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    t_id = int(callback.data.split("_")[2])
    delete_trigger_by_id(t_id, user_id)
    await callback.message.answer("✅ Maxsus so'rov o'chirib tashlandi.")
    await callback.answer()


@dp.callback_query(F.data == "change_text")
async def change_text_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    waiting_for[user_id] = "custom_text"
    await callback.message.answer("📝 Yangi asosiy avto-javob matnini kiriting:")
    await callback.answer()


@dp.callback_query(F.data == "change_delay")
async def change_delay_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    waiting_for[user_id] = "custom_delay"
    await callback.message.answer("⏱ Online bo'lganda necha soniyadan keyin javob qaytarilsin? (Faqat raqam):")
    await callback.answer()


@dp.callback_query(F.data == "start_auto")
async def start_auto_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if has_active_session(user_id):
        await callback.message.edit_text("✅ Siz allaqachon ulangansiz.", reply_markup=get_main_keyboard(True))
        await callback.answer()
        return

    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Telefon raqamini ulashish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await callback.message.edit_text("📱 Telefon raqamingizni ulashing.")
    await callback.message.answer("👇 Tugmani bosing:", reply_markup=keyboard)
    waiting_for[user_id] = "phone"
    await callback.answer()


async def request_code(user_id: int, phone: str, message: types.Message):
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        res = await client.send_code_request(phone)
        
        user_clients[user_id] = client
        phone_cache[user_id] = phone
        phone_code_hash_cache[user_id] = res.phone_code_hash
        waiting_for[user_id] = "code"
        
        await message.answer(
            "✅ Kod yuborildi!\n\n⚠️ Kodni ajratib yuboring (Masalan: <code>5-1-9-9-6</code>).",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )
    except FloodWaitError as e:
        await message.answer(f"⏳ Telegram cheklovi! Biroz kuting: {e.seconds} soniya.")
        waiting_for[user_id] = "phone"
    except Exception as e:
        await message.answer(f"❌ Xatolik: {html.escape(str(e))}")
        waiting_for[user_id] = "phone"


@dp.message(F.contact)
async def handle_contact(message: types.Message):
    user_id = message.from_user.id
    if waiting_for.get(user_id) != "phone":
        await message.answer("❌ /start bosing.")
        return
    phone = message.contact.phone_number
    if not phone.startswith('+'):
        phone = '+' + phone
    await request_code(user_id, phone, message)


@dp.message(F.text & ~F.text.startswith('/'))
async def handle_text_input(message: types.Message):
    user_id = message.from_user.id
    text = message.text
    state = waiting_for.get(user_id)

    # Admin xabar yuborishi
    if state == "admin_broadcast" and is_admin(message.from_user):
        waiting_for[user_id] = None
        users = get_all_sessions()
        count = 0
        for u_id, _ in users:
            try:
                await bot.send_message(u_id, text)
                count += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await message.answer(f"✅ Xabar {count} ta foydalanuvchiga muvaffaqiyatli yuborildi.")
        return

    if state == "trigger_keyword":
        waiting_for[user_id] = f"trigger_response:{text.strip()}"
        await message.answer(f"🔹 **'{text.strip()}'** so'zi yozilganda bot qanday javob qaytarsin?", parse_mode="Markdown")
        return

    elif state and state.startswith("trigger_response:"):
        keyword = state.split(":", 1)[1]
        add_custom_trigger(user_id, keyword, text.strip())
        waiting_for[user_id] = None
        is_connected = has_active_session(user_id)
        await message.answer(
            f"✅ Saqlandi!\n\n🔸 **So'rov:** `{keyword}`\n🔹 **Javob:** `{text.strip()}`",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(is_connected)
        )
        return

    elif state == "custom_text":
        set_custom_reply_text(user_id, text)
        waiting_for[user_id] = None
        is_connected = has_active_session(user_id)
        await message.answer(f"✅ Asosiy javob saqlandi:\n<code>{html.escape(text)}</code>", parse_mode="HTML", reply_markup=get_main_keyboard(is_connected))
        return

    elif state == "custom_delay":
        if text.strip().isdigit():
            sec = int(text.strip())
            if sec < 1:
                await message.answer("❌ Kamida 1 soniya kiritishingiz kerak.")
                return
            set_user_delay(user_id, sec)
            waiting_for[user_id] = None
            is_connected = has_active_session(user_id)
            await message.answer(f"✅ Kutiladigan vaqt <b>{sec} soniya</b> qilib belgilandi!", parse_mode="HTML", reply_markup=get_main_keyboard(is_connected))
        else:
            await message.answer("❌ Faqat raqam kiriting.")
        return

    if state == "phone":
        clean_text = text.strip()
        if clean_text.startswith('+'):
            await request_code(user_id, clean_text, message)
        else:
            await message.answer("❌ Raqamni +998... shaklida kiriting.")

    elif state == "code":
        client = user_clients.get(user_id)
        if not client:
            await message.answer("❌ Xatolik. /start bosing.")
            return

        clean_code = text.replace('-', '').replace(' ', '').strip()
        phone = phone_cache.get(user_id)
        phone_code_hash = phone_code_hash_cache.get(user_id)

        try:
            await client.sign_in(phone=phone, code=clean_code, phone_code_hash=phone_code_hash)
            await finish_login(user_id, client, message)
        except SessionPasswordNeededError:
            waiting_for[user_id] = "password"
            await message.answer("🔐 2-bosqichli parolingizni kiriting:")
        except Exception as e:
            await message.answer(f"❌ Kod noto'g'ri: {html.escape(str(e))}")

    elif state == "password":
        client = user_clients.get(user_id)
        if not client:
            return
        try:
            await client.sign_in(password=text)
            await finish_login(user_id, client, message)
        except Exception as e:
            await message.answer(f"❌ Parol noto'g'ri: {html.escape(str(e))}")


async def finish_login(user_id: int, client: TelegramClient, message: types.Message):
    stop_client_task(user_id)
    session_string = client.session.save()
    save_session(user_id, session_string)
    
    waiting_for[user_id] = None
    phone_cache.pop(user_id, None)
    phone_code_hash_cache.pop(user_id, None)

    task = asyncio.create_task(start_auto_reply(user_id, client))
    user_clients[user_id] = client
    auto_tasks[user_id] = task

    delay = get_user_delay(user_id)
    reply_kb = get_reply_keyboard(message.from_user)
    await message.answer(
        f"✅ Muvaffaqiyatli ulandi!\n🤖 Offline bo'lsangiz darhol, Online bo'lsangiz {delay} soniyada avto-javob yuboriladi va o'qilganida/javob berilganida o'chiriladi.",
        reply_markup=get_main_keyboard(True)
    )
    await message.answer("👇 Qulay boshqaruv menyusi:", reply_markup=reply_kb)


@dp.callback_query(F.data == "stop_auto")
async def stop_auto_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    stop_client_task(user_id)
    delete_session(user_id)
    waiting_for[user_id] = None
    await callback.message.edit_text("✅ Avto-javob to'xtatildi.", reply_markup=get_main_keyboard(False))
    await callback.answer()


# ========== RESTART VAQTI TIKLASH ==========
async def restore_sessions():
    sessions = get_all_sessions()
    for user_id, session_string in sessions:
        try:
            client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                user_clients[user_id] = client
                task = asyncio.create_task(start_auto_reply(user_id, client))
                auto_tasks[user_id] = task
            else:
                await client.disconnect()
                delete_session(user_id)
        except (AuthKeyUnregisteredError, UserDeactivatedBanError, UnauthorizedError):
            delete_session(user_id)
        except Exception as e:
            print(f"Restore error user {user_id}: {e}")
        
        await asyncio.sleep(0.2)


async def main():
    init_db()
    await restore_sessions()
    await bot.delete_webhook(drop_pending_updates=True)
    
    print("🤖 Bot to'liq tayyor!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("To'xtatildi.")
