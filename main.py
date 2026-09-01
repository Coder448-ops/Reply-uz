import asyncio
import os
import sys
import sqlite3
import html
import datetime
from datetime import datetime, timedelta
import re
import traceback
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

# ========== TARJIMA VA TIL ANIQLASH ==========
from deep_translator import GoogleTranslator
from langdetect import detect, DetectorFactory
DetectorFactory.seed = 0

# ========== KONFIGURATSIYA ==========
DB_NAME = 'user_sessions.db'
DEFAULT_REPLY_TEXT = "Hozircha bandman, bo'shashim bilan aloqaga chiqaman."
DEFAULT_DELAY = 7

ADMIN_USERNAME = "NeoPulse_uz"
API_ID = 37437082
API_HASH = "b7d4fa4d28472bf3768a4cae5e3fd01c"
BOT_TOKEN = "8995093768:AAG676LT4-ate2TFoTqHmVbFEDuIZlWsMDc"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ========== XOTIRA ==========
user_clients = {}
auto_tasks = {}
waiting_for = {}
phone_cache = {}
phone_code_hash_cache = {}
chat_log_cache = {}
admin_current_page = {}
BOT_START_TIME = None

# ========== MA'LUMOTLAR BAZASI ==========
def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS sessions
                     (user_id INTEGER PRIMARY KEY, session_string TEXT,
                      custom_text TEXT, delay_seconds INTEGER DEFAULT 7)''')
        c.execute('''CREATE TABLE IF NOT EXISTS custom_triggers
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                      keyword TEXT, response TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS sent_auto_replies
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                      chat_id INTEGER, msg_id INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS holiday_greeted
                     (user_id INTEGER, holiday_name TEXT,
                      greeted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                      PRIMARY KEY (user_id, holiday_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_config
                     (key TEXT PRIMARY KEY, value TEXT)''')
        conn.commit()

def save_session(user_id, session_string):
    with get_db() as conn:
        conn.execute('''INSERT INTO sessions (user_id, session_string)
                        VALUES (?, ?)
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
        conn.execute('''INSERT INTO sessions (user_id, custom_text)
                        VALUES (?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET custom_text=excluded.custom_text''',
                     (user_id, text))

def get_custom_reply_text(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT custom_text FROM sessions WHERE user_id=?', (user_id,))
        row = c.fetchone()
        if row and row[0]:
            return row[0]
    return get_global_default_text()

def set_user_delay(user_id, seconds):
    with get_db() as conn:
        conn.execute('''INSERT INTO sessions (user_id, delay_seconds)
                        VALUES (?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET delay_seconds=excluded.delay_seconds''',
                     (user_id, seconds))

def get_user_delay(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT delay_seconds FROM sessions WHERE user_id=?', (user_id,))
        row = c.fetchone()
        if row and row[0] is not None:
            return row[0]
    return get_global_default_delay()

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
        conn.execute('DELETE FROM holiday_greeted WHERE user_id=?', (user_id,))

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

def get_all_triggers_count():
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM custom_triggers')
        return c.fetchone()[0]

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
    return bool(user and user.username and user.username.lower() == ADMIN_USERNAME.lower())

# ========== GLOBAL SOZLAMALAR (ADMIN BOSHQARADI) ==========
def get_config(key, default=None):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT value FROM bot_config WHERE key=?', (key,))
        row = c.fetchone()
        return row[0] if row else default

def set_config(key, value):
    with get_db() as conn:
        conn.execute('''INSERT INTO bot_config (key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
                     (key, str(value)))

def get_global_default_text():
    return get_config('default_reply_text', DEFAULT_REPLY_TEXT)

def set_global_default_text(text):
    set_config('default_reply_text', text)

def get_global_default_delay():
    return int(get_config('default_delay', DEFAULT_DELAY))

def set_global_default_delay(seconds):
    set_config('default_delay', seconds)

def reset_all_holiday_greetings():
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM holiday_greeted')
        count = c.fetchone()[0]
        conn.execute('DELETE FROM holiday_greeted')
        return count

# ========== HOLIDAY FUNKSIYALARI ==========
def get_holiday_info():
    today = datetime.now()
    month_day = today.strftime("%m-%d")
    holidays = {
        "01-01": ("🎉 Yangi Yil muborak!", "new_year"),
        "03-21": ("🌿 Navro'z muborak!", "navruz"),
        "09-01": ("🇺🇿 Mustaqillik kuni muborak!", "independence_day"),
        "10-01": ("🇺🇿 O'qituvchi va murabbiylar kuni!", "teachers_day"),
        "12-08": ("🇺🇿 Konstitutsiya kuni!", "constitution_day")
    }
    if (today.month == 8 and today.day >= 29) or (today.month == 9 and today.day == 1):
        return ("🇺🇿 Mustaqillik kuni muborak!", "independence_day")
    if month_day in holidays:
        return holidays[month_day]
    return None, None

def has_user_received_holiday(user_id, holiday_name):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT 1 FROM holiday_greeted WHERE user_id=? AND holiday_name=?', (user_id, holiday_name))
        return c.fetchone() is not None

def mark_holiday_greeted(user_id, holiday_name):
    with get_db() as conn:
        conn.execute('INSERT OR IGNORE INTO holiday_greeted (user_id, holiday_name) VALUES (?, ?)',
                     (user_id, holiday_name))

# ========== YORDAMCHI FUNKSIYALAR ==========
def is_silent_hour():
    hour = datetime.now().hour
    return hour >= 22 or hour < 7

def get_time_based_prefix():
    hour = datetime.now().hour
    if 5 <= hour < 11:   return "Xayrli tong"
    if 11 <= hour < 18:  return "Xayrli kun"
    if 18 <= hour < 23:  return "Xayrli kech"
    return "Xayrli tun"

def latin_to_cyrillic_uz(text: str) -> str:
    mapping = {
        'a':'а','b':'б','d':'д','e':'е','f':'ф','g':'г',
        'h':'ҳ','i':'и','j':'ж','k':'к','l':'л','m':'м',
        'n':'н','o':'о','p':'п','q':'қ','r':'р','s':'с',
        't':'т','u':'у','v':'в','x':'х','y':'й','z':'з',
        '‘':'ъ','ʻ':'ъ',
        'A':'А','B':'Б','D':'Д','E':'Е','F':'Ф','G':'Г',
        'H':'Ҳ','I':'И','J':'Ж','K':'К','L':'Л','M':'М',
        'N':'Н','O':'О','P':'П','Q':'Қ','R':'Р','S':'С',
        'T':'Т','U':'У','V':'В','X':'Х','Y':'Й','Z':'З',
    }
    cyrillic = ''
    i = 0
    while i < len(text):
        if i+1 < len(text):
            pair = text[i:i+2].lower()
            if pair == 'sh': cyrillic += 'ш'; i += 2; continue
            if pair == 'ch': cyrillic += 'ч'; i += 2; continue
            if pair == 'ng': cyrillic += 'нг'; i += 2; continue
            if pair == "o'": cyrillic += 'ў'; i += 2; continue
            if pair == "g'": cyrillic += 'ғ'; i += 2; continue
        cyrillic += mapping.get(text[i], text[i])
        i += 1
    return cyrillic

def is_cyrillic_uz(text: str) -> bool:
    return bool(re.search(r'[А-Яа-яЁё]', text))

def detect_language(text: str) -> str:
    try:
        lang = detect(text)
        if lang in ['uz', 'uz-Cyrl']:
            return 'uz'
        elif lang == 'ru':
            return 'ru'
        elif lang == 'en':
            return 'en'
        else:
            return 'uz'
    except:
        return 'uz'

def translate_text(text: str, dest_lang: str) -> str:
    if dest_lang not in ['uz', 'ru', 'en']:
        return text
    try:
        translated = GoogleTranslator(source='auto', target=dest_lang).translate(text)
        return translated
    except Exception as e:
        print(f"Tarjima xatosi: {e}")
        return text

def add_log_entry(user_id, chat_id, from_me, text):
    now = datetime.now()
    entry = {'timestamp': now, 'chat_id': chat_id, 'from_me': from_me, 'text': text}
    chat_log_cache.setdefault(user_id, []).append(entry)
    cutoff = now - timedelta(hours=2)
    chat_log_cache[user_id] = [e for e in chat_log_cache[user_id] if e['timestamp'] >= cutoff]

def get_logs_last_2h(user_id):
    if user_id not in chat_log_cache:
        return []
    cutoff = datetime.now() - timedelta(hours=2)
    return [e for e in chat_log_cache[user_id] if e['timestamp'] >= cutoff]

# ========== UMUMIY JAVOB TAYYORLASH FUNKSIYASI ==========
def get_reply_text(user_id: int, incoming_text: str, is_telethon: bool = False) -> str:
    """Avto-javob matnini tayyorlaydi (bayram, vaqt, prefiks, silent, tarjima, kirill)."""
    my_text = get_custom_reply_text(user_id)

    matched = get_matching_response(user_id, incoming_text)
    reply = matched if matched else my_text

    reply = f"{get_time_based_prefix()}! {reply}"

    greeting, h_name = get_holiday_info()
    if greeting and h_name and not has_user_received_holiday(user_id, h_name):
        reply = f"{greeting}\n\n{reply}"
        mark_holiday_greeted(user_id, h_name)

    reply = f"[🤖 Avto-Javob] {reply}"

    if is_silent_hour():
        reply += "\n\n🌙 Hozir uxlab yotibman, ertalab javob beraman.."

    if is_cyrillic_uz(incoming_text):
        reply = latin_to_cyrillic_uz(reply)

    lang = detect_language(incoming_text)
    if lang in ['uz', 'ru', 'en']:
        reply = translate_text(reply, lang)

    return reply

# ========== TELETHON AVTO JAVOB (faqat shaxsiy xabarlar) ==========
async def start_auto_reply(user_id, client: TelegramClient):
    print(f"✅ [DEBUG] start_auto_reply called for user {user_id}")

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def auto_reply_handler(event):
        print(f"📩 [DEBUG] Private incoming event for user {user_id}: {event.raw_text}")
        try:
            sender = await event.get_sender()
            # Bot hisoblarga avtojavob yubormaymiz (bot-bot loop bo'lmasin)
            if not sender or sender.bot:
                return

            if event.out:
                return

            my_text = get_custom_reply_text(user_id)
            if event.raw_text and event.raw_text.strip() == my_text.strip():
                return

            add_log_entry(user_id, event.chat_id, False, event.raw_text or '')

            full_user = await client(GetFullUserRequest(event.sender_id))
            is_online = isinstance(full_user.users[0].status, UserStatusOnline)
            if is_online:
                delay = get_user_delay(user_id)
                print(f"⏳ [DEBUG] Online, waiting {delay}s")
                await asyncio.sleep(delay)

            reply = get_reply_text(user_id, event.raw_text or '', is_telethon=True)

            await client.send_read_acknowledge(event.chat_id)
            await client.send_action(event.chat_id, 'typing')
            await asyncio.sleep(2)

            sent = await client.send_message(
                event.chat_id, reply,
                reply_to=event.id,
                disable_forwarding=True
            )
            save_sent_reply(user_id, event.chat_id, sent.id)
            print(f"✅ [DEBUG] Reply sent, msg id {sent.id}")

        except (AuthKeyUnregisteredError, UserDeactivatedBanError, UnauthorizedError) as e:
            print(f"❌ [DEBUG] Auth error: {e}")
            stop_client_task(user_id); delete_session(user_id)
        except FloodWaitError as e:
            print(f"⏳ [DEBUG] Flood wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"❌ [DEBUG] Handler error: {e}")
            traceback.print_exc()

    @client.on(events.NewMessage(outgoing=True))
    async def outgoing_handler(event):
        if not event.is_private:
            return
        if event.text and event.text.strip() == ".export_logs":
            logs = get_logs_last_2h(user_id)
            if not logs:
                await client.send_message('me', "📭 So'nggi 2 soat ichida hech qanday xabar mavjud emas.")
                return
            lines = []
            for entry in logs:
                timestamp = entry['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
                who = "Men" if entry['from_me'] else "Suhbatdosh"
                lines.append(f"[{timestamp}] {who}: {entry['text']}")
            await client.send_file(
                'me',
                "\n".join(lines).encode('utf-8'),
                caption="📄 So'nggi 2 soatlik chat tarixi",
                force_document=True
            )
            return

        try:
            chat_id = event.chat_id
            msg_ids = get_and_delete_all_sent_replies(user_id, chat_id)
            if msg_ids:
                await client.delete_messages(chat_id, msg_ids)
        except Exception as e:
            print(f"Avto-javoblarni o'chirishda xatolik: {e}")

        if event.text and not event.text.startswith('.'):
            add_log_entry(user_id, event.chat_id, True, event.text)

    @client.on(events.MessageRead(inbox=True))
    async def read_handler(event):
        try:
            chat_id = event.chat_id
            msg_ids = get_and_delete_all_sent_replies(user_id, chat_id)
            if msg_ids:
                await client.delete_messages(chat_id, msg_ids)
        except Exception as e:
            print(f"Read hodisasida xatolik: {e}")

    try:
        print(f"🔄 [DEBUG] Starting client.run_until_disconnected() for user {user_id}")
        await client.run_until_disconnected()
        print(f"🛑 [DEBUG] client.run_until_disconnected() finished for user {user_id}")
    except Exception as e:
        print(f"❌ [DEBUG] client.run_until_disconnected() error: {e}")
        traceback.print_exc()
    finally:
        print(f"🧹 [DEBUG] Cleaning up user {user_id}")
        user_clients.pop(user_id, None)
        auto_tasks.pop(user_id, None)

def stop_client_task(user_id):
    client = user_clients.get(user_id)
    task = auto_tasks.get(user_id)
    if client:
        asyncio.create_task(client.disconnect())
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
    keyboard = [[KeyboardButton(text="⚙️ Sozlamalar / Bosh menyu")]]
    if is_admin(user):
        keyboard.append([KeyboardButton(text="👑 Admin Panel")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# ========== START VA ADMIN ==========
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    is_connected = has_active_session(user_id)
    if is_connected:
        await message.answer(
            f"Assalomu alaykum! 👋\n\n"
            f"💬 <b>Asosiy avto-javob matningiz:</b>\n<code>{html.escape(get_custom_reply_text(user_id))}</code>\n\n"
            f"⏱ <b>Online kutiladigan vaqt:</b> <code>{get_user_delay(user_id)} soniya</code>\n\n"
            f"Kerakli bo'limni tanlang:",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(True)
        )
    else:
        await message.answer(
            "Assalomu alaykum! 👋\n\n"
            "Botdan foydalanish uchun Telegram akkauntingizni ulang.",
            reply_markup=get_main_keyboard(False)
        )
    await message.answer("👇 Qulay boshqaruv menyusi:", reply_markup=get_reply_keyboard(message.from_user))

@dp.message(F.text == "👑 Admin Panel")
async def admin_panel_cmd(message: types.Message):
    if not is_admin(message.from_user):
        return
    admin_current_page[message.from_user.id] = 0
    await message.answer(
        "🛠 <b>Admin Panel</b>\n\n"
        "Siz bot tizimida admin huquqiga egasiz.\n"
        "Quyidagi tugmani bosing:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Admin menyuni ochish", callback_data="admin_open")]
        ])
    )

async def admin_stats(message: types.Message):
    total_users = get_total_users_count()
    active_sessions = len(get_all_sessions())
    running = len(user_clients)
    triggers = get_all_triggers_count()
    await message.answer(
        f"📊 <b>Bot Statistikasi:</b>\n\n"
        f"👥 Barcha foydalanuvchilar: <b>{total_users}</b>\n"
        f"⚡️ Bazadagi faol seanslar: <b>{active_sessions}</b>\n"
        f"🔄 Hozir ishlab turgan jarayonlar: <b>{running}</b>\n"
        f"🔑 Jami triggerlar: <b>{triggers}</b>",
        parse_mode="HTML"
    )

async def admin_broadcast(message: types.Message):
    waiting_for[message.from_user.id] = "admin_broadcast"
    await message.answer("📝 Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yozing:")

@dp.message(F.text == "⚙️ Sozlamalar / Bosh menyu")
async def settings_cmd(message: types.Message):
    user_id = message.from_user.id
    await message.answer(
        "⚙️ <b>Bosh menyu va sozlamalar:</b>",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(has_active_session(user_id))
    )

# ========== GURUH XABARLARI: O'QIYDI, LEKIN JAVOB BERMAYDI VA LOG CHIQARMAYDI ==========
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_silent_reader(message: types.Message):
    return

# ========== CALLBACKLAR ==========
async def check_login(callback: types.CallbackQuery) -> bool:
    user_id = callback.from_user.id
    if not has_active_session(user_id):
        await callback.answer("❌ Iltimos, avval /start orqali ulaning.", show_alert=True)
        return False
    return True

@dp.callback_query(F.data == "add_trigger")
async def add_trigger_cb(callback: types.CallbackQuery):
    if not await check_login(callback):
        return
    waiting_for[callback.from_user.id] = "trigger_keyword"
    await callback.message.answer(
        "🔹 **Qaysi kichik gap/so'z yozilganda javob berilsin?**\n\nMasalan: `salom` yoki `qayerdasiz`",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "list_triggers")
async def list_triggers_cb(callback: types.CallbackQuery):
    if not await check_login(callback):
        return
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
    await callback.message.answer(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("del_trg_"))
async def delete_trigger_cb(callback: types.CallbackQuery):
    if not await check_login(callback):
        return
    t_id = int(callback.data.split("_")[2])
    delete_trigger_by_id(t_id, callback.from_user.id)
    await callback.message.answer("✅ Maxsus so'rov o'chirib tashlandi.")
    await callback.answer()

@dp.callback_query(F.data == "change_text")
async def change_text_callback(callback: types.CallbackQuery):
    if not await check_login(callback):
        return
    waiting_for[callback.from_user.id] = "custom_text"
    await callback.message.answer("📝 Yangi asosiy avto-javob matnini kiriting:")
    await callback.answer()

@dp.callback_query(F.data == "change_delay")
async def change_delay_callback(callback: types.CallbackQuery):
    if not await check_login(callback):
        return
    waiting_for[callback.from_user.id] = "custom_delay"
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
        resize_keyboard=True, one_time_keyboard=True
    )
    await callback.message.edit_text("📱 Telefon raqamingizni ulashing.")
    await callback.message.answer("👇 Tugmani bosing:", reply_markup=keyboard)
    waiting_for[user_id] = "phone"
    await callback.answer()

@dp.callback_query(F.data == "stop_auto")
async def stop_auto_callback(callback: types.CallbackQuery):
    if not await check_login(callback):
        return
    user_id = callback.from_user.id
    stop_client_task(user_id)
    delete_session(user_id)
    waiting_for[user_id] = None
    await callback.message.edit_text("✅ Avto-javob to'xtatildi.", reply_markup=get_main_keyboard(False))
    await callback.answer()

# ========== ADMIN PANEL (sahifalab ko'rsatiladi) ==========
ADMIN_PAGE_SIZE = 6
USERS_PAGE_SIZE = 10

ADMIN_MENU_ITEMS = [
    ("📊 Statistika", "adm_stats"),
    ("📢 Xabar yuborish (Broadcast)", "adm_broadcast"),
    ("👥 Foydalanuvchilar ro'yxati", "adm_users_list_0"),
    ("🔍 Foydalanuvchi ma'lumoti", "adm_user_info"),
    ("🗑 Foydalanuvchini o'chirish", "adm_user_delete"),
    ("🔑 Umumiy triggerlar soni", "adm_triggers_count"),
    ("⚡️ Faol jarayonlar ro'yxati", "adm_active_sessions"),
    ("🎉 Bayram belgilarini tozalash", "adm_reset_holidays"),
    ("⏱ Standart kechikishni o'zgartirish", "adm_set_default_delay"),
    ("✏️ Standart javob matnini o'zgartirish", "adm_set_default_text"),
    ("⏹ Barcha avto-javoblarni to'xtatish", "adm_stop_all"),
    ("♻️ Botni qayta ishga tushirish", "adm_restart"),
    ("ℹ️ Bot haqida", "adm_about"),
]

def get_admin_keyboard(page: int = 0):
    start = page * ADMIN_PAGE_SIZE
    end = start + ADMIN_PAGE_SIZE
    chunk = ADMIN_MENU_ITEMS[start:end]
    buttons = [[InlineKeyboardButton(text=label, callback_data=cb)] for label, cb in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"adm_page_{page-1}"))
    if end < len(ADMIN_MENU_ITEMS):
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"adm_page_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Yopish", callback_data="adm_close")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(F.data == "admin_open")
async def admin_open_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer()
        return
    admin_current_page[callback.from_user.id] = 0
    await callback.message.edit_text("🛠 <b>Admin Panel</b>\n\nKerakli bo'limni tanlang:", parse_mode="HTML", reply_markup=get_admin_keyboard(0))
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_page_"))
async def admin_page_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer()
        return
    page = int(callback.data.split("_")[-1])
    admin_current_page[callback.from_user.id] = page
    await callback.message.edit_text("🛠 <b>Admin Panel</b>\n\nKerakli bo'limni tanlang:", parse_mode="HTML", reply_markup=get_admin_keyboard(page))
    await callback.answer()

@dp.callback_query(F.data == "adm_close")
async def admin_close_cb(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()

@dp.callback_query(F.data == "adm_stats")
async def adm_stats_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    await admin_stats(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    await admin_broadcast(callback.message)
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_users_list_"))
async def adm_users_list_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    page = int(callback.data.split("_")[-1])
    sessions = get_all_sessions()
    start = page * USERS_PAGE_SIZE
    chunk = sessions[start:start + USERS_PAGE_SIZE]
    if not chunk:
        await callback.message.answer("📭 Hozircha ulangan foydalanuvchilar yo'q.")
        await callback.answer()
        return
    lines = [f"{i + 1 + start}. <code>{uid}</code>" for i, (uid, _) in enumerate(chunk)]
    text = f"👥 <b>Foydalanuvchilar</b> (sahifa {page + 1}):\n\n" + "\n".join(lines)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_users_list_{page-1}"))
    if start + USERS_PAGE_SIZE < len(sessions):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_users_list_{page+1}"))
    markup = InlineKeyboardMarkup(inline_keyboard=[nav]) if nav else None
    await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data == "adm_user_info")
async def adm_user_info_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    waiting_for[callback.from_user.id] = "adm_user_info_input"
    await callback.message.answer("🔍 Ma'lumotini ko'rmoqchi bo'lgan foydalanuvchi ID sini kiriting:")
    await callback.answer()

@dp.callback_query(F.data == "adm_user_delete")
async def adm_user_delete_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    waiting_for[callback.from_user.id] = "adm_user_delete_input"
    await callback.message.answer("🗑 O'chiriladigan foydalanuvchi ID sini kiriting:")
    await callback.answer()

@dp.callback_query(F.data == "adm_triggers_count")
async def adm_triggers_count_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    count = get_all_triggers_count()
    await callback.message.answer(f"🔑 Jami triggerlar soni: <b>{count}</b>", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "adm_active_sessions")
async def adm_active_sessions_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    ids = list(user_clients.keys())
    if not ids:
        await callback.message.answer("📭 Hozircha faol jarayon yo'q.")
    else:
        lines = "\n".join(f"• <code>{i}</code>" for i in ids)
        await callback.message.answer(f"⚡️ <b>Faol jarayonlar ({len(ids)}):</b>\n\n{lines}", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "adm_reset_holidays")
async def adm_reset_holidays_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    count = reset_all_holiday_greetings()
    await callback.message.answer(f"🎉 {count} ta bayram belgisi tozalandi. Endi foydalanuvchilar qayta tabriklanadi.")
    await callback.answer()

@dp.callback_query(F.data == "adm_set_default_delay")
async def adm_set_default_delay_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    waiting_for[callback.from_user.id] = "adm_default_delay_input"
    await callback.message.answer(f"⏱ Joriy standart kechikish: {get_global_default_delay()} soniya.\nYangi qiymatni kiriting (faqat raqam):")
    await callback.answer()

@dp.callback_query(F.data == "adm_set_default_text")
async def adm_set_default_text_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    waiting_for[callback.from_user.id] = "adm_default_text_input"
    await callback.message.answer(
        f"✏️ Joriy standart matn:\n<code>{html.escape(get_global_default_text())}</code>\n\nYangisini kiriting:",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_stop_all")
async def adm_stop_all_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    ids = list(user_clients.keys())
    for uid in ids:
        stop_client_task(uid)
    await callback.message.answer(f"⏹ {len(ids)} ta faol avto-javob jarayoni to'xtatildi (seanslar o'chirilmadi, keyingi qayta ishga tushirishda tiklanadi).")
    await callback.answer()

@dp.callback_query(F.data == "adm_restart")
async def adm_restart_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    await callback.message.answer("♻️ Bot qayta ishga tushmoqda...")
    await callback.answer()
    os.execv(sys.executable, [sys.executable] + sys.argv)

@dp.callback_query(F.data == "adm_about")
async def adm_about_cb(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer(); return
    uptime = datetime.now() - BOT_START_TIME if BOT_START_TIME else timedelta(0)
    total_users = get_total_users_count()
    active = len(user_clients)
    await callback.message.answer(
        f"ℹ️ <b>Bot haqida</b>\n\n"
        f"👥 Jami foydalanuvchilar: {total_users}\n"
        f"⚡️ Faol jarayonlar: {active}\n"
        f"⏳ Ishlab turgan vaqti: {str(uptime).split('.')[0]}",
        parse_mode="HTML"
    )
    await callback.answer()

# ========== AIOGRAM AVTO-JAVOB (SHaxsiy xabarlar) ==========
@dp.message(F.chat.type == "private", F.text)
async def aiogram_auto_reply(message: types.Message):
    user_id = message.from_user.id
    text = message.text

    # Bot hisoblarga avtojavob yubormaymiz
    if message.from_user.is_bot:
        return

    if not has_active_session(user_id):
        return

    state = waiting_for.get(user_id)
    if state in ["trigger_keyword", "trigger_response:", "custom_text", "custom_delay",
                 "adm_user_info_input", "adm_user_delete_input",
                 "adm_default_delay_input", "adm_default_text_input"]:
        return

    if state == "admin_broadcast":
        return

    if text in ["⚙️ Sozlamalar / Bosh menyu", "👑 Admin Panel"]:
        return

    reply = get_reply_text(user_id, text, is_telethon=False)

    await bot.send_chat_action(message.chat.id, action="typing")
    await asyncio.sleep(2)

    await message.answer(reply)

# ========== MATN ISHLOVCHI (sozlamalar va maxsus holatlar) ==========
@dp.message(F.text & ~F.text.startswith('/'))
async def handle_text_input(message: types.Message):
    user_id = message.from_user.id
    text = message.text
    state = waiting_for.get(user_id)

    # ---- ADMIN holatlari ----
    if state == "admin_broadcast" and is_admin(message.from_user):
        waiting_for[user_id] = None
        users = get_all_sessions()
        count = 0
        for u_id, _ in users:
            try:
                await bot.send_message(u_id, text)
                count += 1
                await asyncio.sleep(0.05)
            except:
                pass
        await message.answer(f"✅ Xabar {count} ta foydalanuvchiga yuborildi.")
        return

    if state == "adm_user_info_input" and is_admin(message.from_user):
        waiting_for[user_id] = None
        try:
            target_id = int(text.strip())
        except ValueError:
            await message.answer("❌ Foydalanuvchi ID raqam bo'lishi kerak.")
            return
        if not has_active_session(target_id):
            await message.answer("❌ Bunday ulangan foydalanuvchi topilmadi.")
            return
        triggers = get_user_triggers(target_id)
        is_running = target_id in user_clients
        info = (
            f"🔍 <b>Foydalanuvchi:</b> <code>{target_id}</code>\n"
            f"💬 Javob matni: <code>{html.escape(get_custom_reply_text(target_id))}</code>\n"
            f"⏱ Kechikish: {get_user_delay(target_id)} soniya\n"
            f"🔑 Triggerlar soni: {len(triggers)}\n"
            f"⚡️ Faol jarayon: {'✅ Ha' if is_running else '❌ Yoq'}"
        )
        await message.answer(info, parse_mode="HTML")
        return

    if state == "adm_user_delete_input" and is_admin(message.from_user):
        waiting_for[user_id] = None
        try:
            target_id = int(text.strip())
        except ValueError:
            await message.answer("❌ Foydalanuvchi ID raqam bo'lishi kerak.")
            return
        stop_client_task(target_id)
        delete_session(target_id)
        await message.answer(f"✅ Foydalanuvchi <code>{target_id}</code> o'chirildi.", parse_mode="HTML")
        return

    if state == "adm_default_delay_input" and is_admin(message.from_user):
        waiting_for[user_id] = None
        if text.strip().isdigit() and int(text.strip()) >= 1:
            set_global_default_delay(int(text.strip()))
            await message.answer(f"✅ Standart kechikish {text.strip()} soniya qilib belgilandi.")
        else:
            await message.answer("❌ Faqat 1 dan katta raqam kiriting.")
        return

    if state == "adm_default_text_input" and is_admin(message.from_user):
        waiting_for[user_id] = None
        set_global_default_text(text)
        await message.answer(
            f"✅ Standart javob matni yangilandi:\n<code>{html.escape(text)}</code>",
            parse_mode="HTML"
        )
        return

    # ---- Ulanish jarayoni ----
    if state == "phone":
        if text.startswith('+'):
            await request_code(user_id, text, message)
        else:
            await message.answer("❌ Raqamni +998... shaklida kiriting.")
        return

    if state == "code":
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
        return

    if state == "password":
        client = user_clients.get(user_id)
        if not client:
            return
        try:
            await client.sign_in(password=text)
            await finish_login(user_id, client, message)
        except Exception as e:
            await message.answer(f"❌ Parol noto'g'ri: {html.escape(str(e))}")
        return

    if not has_active_session(user_id):
        await message.answer("❌ Botdan foydalanish uchun avval /start buyrug'i orqali ulaning.")
        return

    # ---- Trigger qo'shish ----
    if state == "trigger_keyword":
        waiting_for[user_id] = f"trigger_response:{text.strip()}"
        await message.answer(f"🔹 **'{text.strip()}'** so'zi yozilganda bot qanday javob qaytarsin?", parse_mode="Markdown")
        return

    if state and state.startswith("trigger_response:"):
        keyword = state.split(":", 1)[1]
        add_custom_trigger(user_id, keyword, text.strip())
        waiting_for[user_id] = None
        await message.answer(
            f"✅ Saqlandi!\n\n🔸 **So'rov:** `{keyword}`\n🔹 **Javob:** `{text.strip()}`",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(True)
        )
        return

    if state == "custom_text":
        set_custom_reply_text(user_id, text)
        waiting_for[user_id] = None
        await message.answer(
            f"✅ Asosiy javob saqlandi:\n<code>{html.escape(text)}</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(True)
        )
        return

    if state == "custom_delay":
        if text.strip().isdigit():
            sec = int(text.strip())
            if sec < 1:
                await message.answer("❌ Kamida 1 soniya kiritishingiz kerak.")
                return
            set_user_delay(user_id, sec)
            waiting_for[user_id] = None
            await message.answer(
                f"✅ Kutiladigan vaqt <b>{sec} soniya</b> qilib belgilandi!",
                parse_mode="HTML",
                reply_markup=get_main_keyboard(True)
            )
        else:
            await message.answer("❌ Faqat raqam kiriting.")
        return

# ========== QOLGAN FUNKSIYALAR (request_code, finish_login, restore_sessions, main) ==========
async def request_code(user_id, phone, message):
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

async def finish_login(user_id, client, message):
    stop_client_task(user_id)
    session_string = client.session.save()
    save_session(user_id, session_string)
    waiting_for[user_id] = None
    phone_cache.pop(user_id, None)
    phone_code_hash_cache.pop(user_id, None)

    task = asyncio.create_task(start_auto_reply(user_id, client))
    user_clients[user_id] = client
    auto_tasks[user_id] = task
    print(f"✅ [DEBUG] start_auto_reply task created for user {user_id}")

    await message.answer(
        f"✅ Muvaffaqiyatli ulandi!\n🤖 Offline bo'lsangiz darhol, Online bo'lsangiz {get_user_delay(user_id)} soniyada avto-javob yuboriladi.",
        reply_markup=get_main_keyboard(True)
    )
    await message.answer("👇 Qulay boshqaruv menyusi:", reply_markup=get_reply_keyboard(message.from_user))

async def restore_sessions():
    sessions = get_all_sessions()
    for user_id, session_string in sessions:
        try:
            client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                user_clients[user_id] = client
                auto_tasks[user_id] = asyncio.create_task(start_auto_reply(user_id, client))
                print(f"♻️ [DEBUG] Restored session for user {user_id}")
            else:
                await client.disconnect()
                delete_session(user_id)
        except (AuthKeyUnregisteredError, UserDeactivatedBanError, UnauthorizedError):
            delete_session(user_id)
        except Exception as e:
            print(f"Restore error user {user_id}: {e}")
        await asyncio.sleep(0.2)

async def main():
    global BOT_START_TIME
    BOT_START_TIME = datetime.now()
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
