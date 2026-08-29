import os
import asyncio
import sqlite3
import sys
from dotenv import load_dotenv, set_key
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, KeyboardButton, ReplyKeyboardMarkup
from aiogram import F
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
    PhoneNumberInvalidError,
)
from telethon.tl.types import User

DB_NAME = 'user_sessions.db'
ENV_FILE = '.env'
DEFAULT_REPLY_TEXT = "Kechirasiz, hozircha bandman. Kelishim bilan javob beraman.\n(Avtomatik javob)"

# ========== GLOBAL SOZLAMALAR ==========
API_ID = None
API_HASH = None
BOT_TOKEN = None

bot: Bot = None
dp = Dispatcher()

# ========== XOTIRA ==========
user_clients = {}          # user_id -> TelegramClient
auto_tasks = {}            # user_id -> asyncio.Task
waiting_for = {}           # user_id -> "phone" | "code" | "password" | "custom_text" | None
phone_cache = {}           # user_id -> phone
phone_code_hash_cache = {} # user_id -> phone_code_hash


# ========== MA'LUMOTLARNI .ENV FAYLDAN OLISH YOKI SAQLASH ==========
def load_or_get_credentials():
    global API_ID, API_HASH, BOT_TOKEN

    load_dotenv(ENV_FILE)

    API_ID = os.getenv("API_ID")
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")

    if API_ID and API_HASH and BOT_TOKEN:
        API_ID = int(API_ID)
        print("=" * 50)
        print("✅ Saqlangan sozlamalar (.env) muvaffaqiyatli yuklandi!")
        print("=" * 50)
        return

    print("=" * 50)
    print("🤖 Birinchi marta ishga tushirish. Sozlamalarni kiriting:")
    print("=" * 50)

    while True:
        api_id_input = input("👉 API_ID ni kiriting: ").strip()
        if api_id_input.isdigit():
            API_ID = int(api_id_input)
            break
        print("❌ API_ID faqat raqamlardan iborat bo'lishi kerak.")

    while True:
        api_hash_input = input("👉 API_HASH ni kiriting: ").strip()
        if len(api_hash_input) > 10:
            API_HASH = api_hash_input
            break
        print("❌ API_HASH noto'g'ri ko'rinadi.")

    while True:
        bot_token_input = input("👉 BOT_TOKEN ni kiriting: ").strip()
        if ":" in bot_token_input and len(bot_token_input) > 20:
            BOT_TOKEN = bot_token_input
            break
        print("❌ BOT_TOKEN noto'g'ri ko'rinadi.")

    with open(ENV_FILE, "a", encoding="utf-8") as f:
        pass

    set_key(ENV_FILE, "API_ID", str(API_ID))
    set_key(ENV_FILE, "API_HASH", str(API_HASH))
    set_key(ENV_FILE, "BOT_TOKEN", str(BOT_TOKEN))

    print("=" * 50)
    print("✅ Ma'lumotlar .env fayliga saqlandi!")
    print("=" * 50)


# ========== SQLITE FUNKSIYALARI ==========
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions
                 (user_id INTEGER PRIMARY KEY, session_string TEXT, custom_text TEXT)''')
    
    # Eski baza bo'lsa custom_text ustunini qo'shadi
    c.execute("PRAGMA table_info(sessions)")
    columns = [column[1] for column in c.fetchall()]
    if 'custom_text' not in columns:
        c.execute("ALTER TABLE sessions ADD COLUMN custom_text TEXT")
        print("🛠️ Ma'lumotlar bazasiga 'custom_text' ustuni qo'shildi.")
        
    conn.commit()
    conn.close()


def save_session(user_id, session_string):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''INSERT INTO sessions (user_id, session_string) VALUES (?, ?)
                 ON CONFLICT(user_id) DO UPDATE SET session_string=excluded.session_string''', 
              (user_id, session_string))
    conn.commit()
    conn.close()


def set_custom_reply_text(user_id, text):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''INSERT INTO sessions (user_id, custom_text) VALUES (?, ?)
                 ON CONFLICT(user_id) DO UPDATE SET custom_text=excluded.custom_text''', 
              (user_id, text))
    conn.commit()
    conn.close()


def get_custom_reply_text(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT custom_text FROM sessions WHERE user_id=?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        return row[0]
    return DEFAULT_REPLY_TEXT


def get_session(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT session_string FROM sessions WHERE user_id=?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def get_all_sessions():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT user_id, session_string FROM sessions WHERE session_string IS NOT NULL')
    rows = c.fetchall()
    conn.close()
    return rows


def delete_session(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()


# ========== AVTOMATIK JAVOB FUNKSIYASI ==========
async def start_auto_reply(user_id, client: TelegramClient):
    @client.on(events.NewMessage(incoming=True))
    async def auto_reply_handler(event):
        if not event.is_private or event.out:
            return
            
        sender = await event.get_sender()
        if not isinstance(sender, User) or sender.bot or sender.is_self:
            return

        chat_id = event.chat_id

        # 1. 7 soniya kutamiz
        await asyncio.sleep(7)

        # 2. Shu 7 soniya ichida o'zingiz chatga javob yozgan bo'lsangiz, avto-javob yuborilmaydi
        try:
            async for msg in client.iter_messages(chat_id, limit=3):
                if msg.out:
                    return
        except Exception:
            pass

        # 3. Chat dialog holatini tekshirish: unread_count 0 bo'lsa (o'qilgan bo'lsa) to'xtaymiz
        try:
            dialogs = await client.get_dialogs(limit=50)
            target_dialog = None
            for d in dialogs:
                if d.entity.id == sender.id:
                    target_dialog = d
                    break

            if target_dialog and target_dialog.unread_count == 0:
                return
        except Exception as e:
            print(f"Dialog tekshirishda xato: {e}")

        # 4. Hali ham o'qilmagan bo'lsa, foydalanuvchining shaxsiy matnini yuboramiz
        reply_text = get_custom_reply_text(user_id)

        try:
            await event.reply(reply_text)
            print(f"📩 Avtomatik javob yuborildi (7s o'qilmadi): {sender.first_name}")
        except Exception as e:
            print(f"Xatolik (auto reply): {e}")

    try:
        if not client.is_connected():
            await client.connect()
        print(f"✅ User {user_id} uchun auto reply ishga tushdi.")
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"❌ Auto reply to‘xtadi (xato): {e}")
    finally:
        print(f"ℹ️ User {user_id} uchun auto reply tugatildi.")
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


# ========== BOT BUYRUQLARI ==========
def get_main_keyboard(is_connected=False):
    buttons = []
    if is_connected:
        buttons.append([InlineKeyboardButton(text="✏️ Avto-javob matnini o'zgartirish", callback_data="change_text")])
        buttons.append([InlineKeyboardButton(text="⏹ Avto-javobni to'xtatish", callback_data="stop_auto")])
    else:
        buttons.append([InlineKeyboardButton(text="🚀 Boshlash (Ulanish)", callback_data="start_auto")])
        buttons.append([InlineKeyboardButton(text="✏️ Avto-javob matnini o'zgartirish", callback_data="change_text")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    is_connected = user_id in user_clients and user_clients[user_id].is_connected()
    current_text = get_custom_reply_text(user_id)

    await message.answer(
        f"Assalomu alaykum! 👋\n\n"
        f"💬 **Sizning joriy avto-javob matningiz:**\n`{current_text}`\n\n"
        f"Kerakli bo'limni tanlang:",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(is_connected)
    )


@dp.callback_query(F.data == "change_text")
async def change_text_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    waiting_for[user_id] = "custom_text"
    
    await callback.message.answer(
        "📝 Yangi avto-javob matningizni yozib yuboring:\n\n"
        "*(Masalan: Hozir bandman, tez orada javob beraman!)*",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "start_auto")
async def start_auto_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if user_id in user_clients and user_clients[user_id].is_connected():
        await callback.message.edit_text(
            "✅ Siz allaqachon ulangansiz. Avtomatik javob faol.",
            reply_markup=get_main_keyboard(True)
        )
        await callback.answer()
        return

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Telefon raqamini ulashish", request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await callback.message.edit_text(
        "📱 Iltimos, quyidagi tugma orqali telefon raqamingizni ulashing."
    )
    await callback.message.answer(
        "👇 Tugmani bosing:",
        reply_markup=keyboard
    )
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
            "✅ Kod yuborildi!\n\n"
            "⚠️ **MUHIM:** Kodni quyidagi formatda yuboring:\n"
            "👉 `(1-2-3-4-5)` masalan kod `51996` bo'lsa `5-1-9-9-6` deb yuboring.",
            parse_mode="Markdown",
            reply_markup=types.ReplyKeyboardRemove()
        )
    except FloodWaitError as e:
        await message.answer(f"⏳ Juda ko'p urinish. {e.seconds} soniyadan keyin urinib ko'ring.")
        waiting_for[user_id] = "phone"
    except PhoneNumberInvalidError:
        await message.answer("❌ Telefon raqam noto'g'ri. Qaytadan kiriting (+998901234567).")
        waiting_for[user_id] = "phone"
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}. Iltimos, to'g'ri raqam kiriting.")
        waiting_for[user_id] = "phone"


@dp.message(F.contact)
async def handle_contact(message: types.Message):
    user_id = message.from_user.id
    contact = message.contact

    if waiting_for.get(user_id) != "phone":
        await message.answer("❌ Iltimos, avval /start bosing va 'Boshlash' tugmasini bosing.")
        return

    phone = contact.phone_number
    if not phone:
        await message.answer("❌ Telefon raqam topilmadi.")
        return

    if not phone.startswith('+'):
        phone = '+' + phone

    await request_code(user_id, phone, message)


@dp.message(F.text & ~F.text.startswith('/'))
async def handle_text_input(message: types.Message):
    user_id = message.from_user.id
    text = message.text
    state = waiting_for.get(user_id)

    if state == "custom_text":
        set_custom_reply_text(user_id, text)
        waiting_for[user_id] = None
        is_connected = user_id in user_clients and user_clients[user_id].is_connected()
        await message.answer(
            f"✅ **Sizning shaxsiy avto-javob matningiz saqlandi!**\n\n"
            f"📝 **Yangi matn:**\n`{text}`",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(is_connected)
        )
        return

    if user_id in user_clients and user_clients[user_id].is_connected() and state is None:
        await message.answer("✅ Siz allaqachon ulangansiz.", reply_markup=get_main_keyboard(True))
        return

    if state == "phone":
        clean_text = text.strip()
        if clean_text.startswith('+') and clean_text[1:].replace(' ', '').isdigit():
            await request_code(user_id, clean_text, message)
        else:
            await message.answer("❌ Noto'g'ri format. Iltimos, +998901234567 ko'rinishida kiriting.")

    elif state == "code":
        client = user_clients.get(user_id)
        if not client:
            await message.answer("❌ Xatolik yuz berdi. Iltimos, /start bosing.")
            waiting_for[user_id] = None
            return

        clean_code = (
            text.replace(' ', '')
            .replace('-', '')
            .replace('(', '')
            .replace(')', '')
            .replace('.', '')
            .strip()
        )
        phone = phone_cache.get(user_id)
        phone_code_hash = phone_code_hash_cache.get(user_id)

        try:
            await client.sign_in(phone=phone, code=clean_code, phone_code_hash=phone_code_hash)
            await finish_login(user_id, client, message)

        except SessionPasswordNeededError:
            waiting_for[user_id] = "password"
            await message.answer("🔐 Sizda 2-bosqichli parol yoqilgan. Parolingizni kiriting:")

        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            await message.answer("❌ Kod noto'g'ri yoki muddati o'tgan. Qaytadan /start bosing.")
            await client.disconnect()
            user_clients.pop(user_id, None)
            phone_code_hash_cache.pop(user_id, None)
            waiting_for[user_id] = "phone"

        except Exception as e:
            await message.answer(f"❌ Xatolik: {e}. Qaytadan /start bosing.")
            await client.disconnect()
            user_clients.pop(user_id, None)
            phone_code_hash_cache.pop(user_id, None)
            waiting_for[user_id] = "phone"

    elif state == "password":
        client = user_clients.get(user_id)
        if not client:
            await message.answer("❌ Xatolik yuz berdi. Iltimos, /start bosing.")
            waiting_for[user_id] = None
            return
        try:
            await client.sign_in(password=text)
            await finish_login(user_id, client, message)
        except Exception as e:
            await message.answer(f"❌ Parol noto'g'ri: {e}. Qaytadan kiriting.")

    else:
        await message.answer("❌ Iltimos, avval /start bosing.", reply_markup=get_main_keyboard(False))


async def finish_login(user_id: int, client: TelegramClient, message: types.Message):
    session_string = client.session.save()
    save_session(user_id, session_string)
    waiting_for[user_id] = None
    phone_cache.pop(user_id, None)
    phone_code_hash_cache.pop(user_id, None)

    task = asyncio.create_task(start_auto_reply(user_id, client))
    auto_tasks[user_id] = task

    await message.answer(
        "✅ Muvaffaqiyatli ulandi!\n"
        "🤖 Endi 7 soniya ichida o'qilmagan xabarlarga avtomatik javob beriladi.",
        reply_markup=get_main_keyboard(True)
    )


@dp.callback_query(F.data == "stop_auto")
async def stop_auto_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if user_id in user_clients or get_session(user_id):
        stop_client_task(user_id)
        delete_session(user_id)
        waiting_for[user_id] = None
        await callback.message.edit_text("✅ Avtomatik javob to'xtatildi va sessiya o'chirildi.", reply_markup=get_main_keyboard(False))
    else:
        await callback.message.edit_text("❌ Siz hali ulanmagansiz.", reply_markup=get_main_keyboard(False))
    await callback.answer()


# ========== SAQLANGAN SESSIYALARNI TIKLASH ==========
async def restore_sessions():
    for user_id, session_string in get_all_sessions():
        try:
            client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                user_clients[user_id] = client
                task = asyncio.create_task(start_auto_reply(user_id, client))
                auto_tasks[user_id] = task
                print(f"🔄 User {user_id} sessiyasi tiklandi.")
            else:
                await client.disconnect()
                delete_session(user_id)
                print(f"⚠️ User {user_id} sessiyasi yaroqsiz, o'chirildi.")
        except Exception as e:
            print(f"❌ Sessiyani tiklashda xato (user {user_id}): {e}")
            delete_session(user_id)


# ========== BOTNI ISHGA TUSHIRISH ==========
async def main():
    global bot

    load_or_get_credentials()

    bot = Bot(token=BOT_TOKEN)

    try:
        bot_info = await bot.get_me()
        print(f"✅ Bot muvaffaqiyatli ulandi: @{bot_info.username}")
    except Exception as e:
        print(f"❌ BOT_TOKEN noto'g'ri yoki xatolik: {e}")
        sys.exit(1)

    init_db()
    await restore_sessions()

    print("🤖 Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot to'xtatildi.")
