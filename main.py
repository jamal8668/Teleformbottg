# main.py
# Teleform — исправленная версия без функции "обычная заявка" (только через подключённые каналы)
# Добавлен webhook (Flask). Токен берётся из переменных окружения или из заданного по умолчанию.
# Требует: pip install pyTelegramBotAPI Flask gunicorn

import os
import sqlite3
import time
import logging
from datetime import timedelta
from flask import Flask, request, abort

import telebot
from telebot import types

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== НАСТРОЙКИ (токен из env или значение по умолчанию) ==========
# Если хочешь хранить токен в env — задай BOT_TOKEN в Render. Если нет, будет использован токен ниже.
TOKEN = os.environ.get("BOT_TOKEN", "8419255009:AAES3WkfbLW9Gd1JrZiN8x5hQHFGA0EaRD0")
# Укажи публичный URL вашего сервиса (Render) в WEBHOOK_URL env, например https://your-service.onrender.com
WEBHOOK_BASE = os.environ.get("WEBHOOK_URL", "https://your-service.onrender.com")
PORT = int(os.environ.get("PORT", 5000))

COOLDOWN_SECONDS = 3600  # 1 час per-channel
MAX_TEXT_LENGTH = 4000  # допустимая длина текста
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
DB_PATH = "teleform_full_v2.db"

# Создаём бота (webhook mode)
bot = telebot.TeleBot(TOKEN)

# BOT username (для deep links)
try:
    BOT_USERNAME = bot.get_me().username
except Exception:
    BOT_USERNAME = None

# ========== БД (с таймаутом) ==========
db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
cur = db.cursor()

# channels: owner_id — тот, кто подключил канал
cur.execute('''
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER,
    channel_id TEXT,
    title TEXT,
    created_at INTEGER
)
''')

# гарантируем уникальность channel_id (чтобы не добавлять один и тот же канал несколько раз)
cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_channel_id ON channels(channel_id)")

# channel_admins: модераторы канала (owner может добавить нескольких)
cur.execute('''
CREATE TABLE IF NOT EXISTS channel_admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_dbid INTEGER,
    admin_user_id INTEGER,
    added_by INTEGER,
    created_at INTEGER,
    UNIQUE(channel_dbid, admin_user_id)
)
''')

# submissions: заявки от пользователей
cur.execute('''
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    content_type TEXT,
    text_content TEXT,
    file_id TEXT,
    status TEXT,
    created_at INTEGER,
    anonymous INTEGER DEFAULT 1,
    target_channel_dbid INTEGER DEFAULT 0
)
''')

# cooldowns: когда пользователь в последний раз успешно публиковал в канал
cur.execute('''
CREATE TABLE IF NOT EXISTS cooldowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    channel_dbid INTEGER,
    last_ts INTEGER,
    UNIQUE(user_id, channel_dbid)
)
''')

# persistent user states (замена in-memory user_state)
cur.execute('''
CREATE TABLE IF NOT EXISTS user_states (
    user_id INTEGER PRIMARY KEY,
    state TEXT,
    updated_at INTEGER
)
''')

# bans: локальные баны по каналу
cur.execute('''
CREATE TABLE IF NOT EXISTS bans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_dbid INTEGER,
    user_id INTEGER,
    added_by INTEGER,
    created_at INTEGER,
    UNIQUE(channel_dbid, user_id)
)
''')

# submission_actions: лог действий модераторов (accept/reject/publish/reply)
cur.execute('''
CREATE TABLE IF NOT EXISTS submission_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER,
    moderator_id INTEGER,
    action TEXT,
    note TEXT,
    created_at INTEGER
)
''')

db.commit()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def now_ts():
    return int(time.time())

# state persistence
def set_state(user_id, state):
    ts = now_ts()
    try:
        cur.execute("INSERT OR REPLACE INTO user_states (user_id, state, updated_at) VALUES (?, ?, ?)", (user_id, state, ts))
        db.commit()
    except Exception:
        pass

def get_state(user_id):
    cur.execute("SELECT state FROM user_states WHERE user_id = ?", (user_id,))
    r = cur.fetchone()
    return r[0] if r else None

def pop_state(user_id):
    cur.execute("SELECT state FROM user_states WHERE user_id = ?", (user_id,))
    r = cur.fetchone()
    if not r:
        return None
    state = r[0]
    cur.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))
    db.commit()
    return state

# channels
def add_channel(owner_id, channel_id, title):
    ts = now_ts()
    key = str(channel_id)
    # проверка существующего канала (защита от дублирования)
    cur.execute("SELECT id FROM channels WHERE channel_id = ?", (key,))
    existing = cur.fetchone()
    if existing:
        return existing[0]
    try:
        cur.execute("INSERT INTO channels (owner_id, channel_id, title, created_at) VALUES (?, ?, ?, ?)", (owner_id, key, title, ts))
        db.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        # если уникальность нарушена параллельно — вернём существующий id
        cur.execute("SELECT id FROM channels WHERE channel_id = ?", (key,))
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None

def list_channels_by_owner(owner_id):
    cur.execute("SELECT id, channel_id, title FROM channels WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,))
    return cur.fetchall()

def get_channel_by_dbid(dbid):
    cur.execute("SELECT id, owner_id, channel_id, title FROM channels WHERE id = ?", (dbid,))
    return cur.fetchone()

def remove_channel(dbid):
    cur.execute("DELETE FROM channels WHERE id = ?", (dbid,))
    cur.execute("DELETE FROM channel_admins WHERE channel_dbid = ?", (dbid,))
    cur.execute("DELETE FROM bans WHERE channel_dbid = ?", (dbid,))
    db.commit()

# channel admins
def add_channel_admin(channel_dbid, admin_user_id, added_by):
    ts = now_ts()
    try:
        cur.execute("INSERT INTO channel_admins (channel_dbid, admin_user_id, added_by, created_at) VALUES (?, ?, ?, ?)",
                    (channel_dbid, admin_user_id, added_by, ts))
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def list_channel_admins(channel_dbid):
    cur.execute("SELECT admin_user_id FROM channel_admins WHERE channel_dbid = ?", (channel_dbid,))
    return [r[0] for r in cur.fetchall()]

def remove_channel_admin(channel_dbid, admin_user_id):
    cur.execute("DELETE FROM channel_admins WHERE channel_dbid = ? AND admin_user_id = ?", (channel_dbid, admin_user_id))
    db.commit()

# submissions
def save_submission(user_id, content_type, text_content, file_id, anonymous, target_channel_dbid=0):
    ts = now_ts()
    cur.execute("INSERT INTO submissions (user_id, content_type, text_content, file_id, status, created_at, anonymous, target_channel_dbid) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, content_type, text_content, file_id, "pending", ts, 1 if anonymous else 0, target_channel_dbid))
    db.commit()
    return cur.lastrowid

def get_submission(sub_id):
    cur.execute("SELECT id, user_id, content_type, text_content, file_id, status, created_at, anonymous, target_channel_dbid FROM submissions WHERE id = ?", (sub_id,))
    return cur.fetchone()

def set_submission_status(sub_id, status, moderator_id=None, note=None):
    ts = now_ts()
    cur.execute("UPDATE submissions SET status = ? WHERE id = ?", (status, sub_id))
    if moderator_id:
        try:
            cur.execute("INSERT INTO submission_actions (submission_id, moderator_id, action, note, created_at) VALUES (?, ?, ?, ?, ?)",
                        (sub_id, moderator_id, status, note or "", ts))
        except Exception:
            pass
    db.commit()

# cooldowns
def set_cooldown(user_id, channel_dbid, ts=None):
    ts = ts or now_ts()
    try:
        cur.execute("INSERT INTO cooldowns (user_id, channel_dbid, last_ts) VALUES (?, ?, ?)", (user_id, channel_dbid, ts))
    except Exception:
        cur.execute("UPDATE cooldowns SET last_ts = ? WHERE user_id = ? AND channel_dbid = ?", (ts, user_id, channel_dbid))
    db.commit()

def get_last_published(user_id, channel_dbid):
    cur.execute("SELECT last_ts FROM cooldowns WHERE user_id = ? AND channel_dbid = ?", (user_id, channel_dbid))
    r = cur.fetchone()
    return r[0] if r else None

# bans
def add_ban(channel_dbid, user_id, added_by):
    ts = now_ts()
    try:
        cur.execute("INSERT INTO bans (channel_dbid, user_id, added_by, created_at) VALUES (?, ?, ?, ?)", (channel_dbid, user_id, added_by, ts))
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def remove_ban(channel_dbid, user_id):
    cur.execute("DELETE FROM bans WHERE channel_dbid = ? AND user_id = ?", (channel_dbid, user_id))
    db.commit()

def is_banned(channel_dbid, user_id):
    cur.execute("SELECT 1 FROM bans WHERE channel_dbid = ? AND user_id = ?", (channel_dbid, user_id))
    return bool(cur.fetchone())

# formatting
def format_timedelta_seconds(sec):
    if sec <= 0:
        return "0s"
    td = timedelta(seconds=sec)
    hours = td.seconds // 3600 + td.days * 24
    minutes = (td.seconds % 3600) // 60
    seconds = td.seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

# ========== МАРКАПЫ ==========
def main_menu():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📩 Предложить пост", callback_data="menu_offer"))
    kb.add(types.InlineKeyboardButton("🔧 Управление каналами", callback_data="menu_channels"))
    kb.add(types.InlineKeyboardButton("ℹ️ Помощь", callback_data="menu_help"))
    return kb

def channels_menu():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Подключить канал", callback_data="add_channel"))
    kb.add(types.InlineKeyboardButton("📋 Мои каналы", callback_data="my_channels"))
    kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="menu_back"))
    return kb

# ========== START / MENU ==========
@bot.message_handler(commands=["start"])
def cmd_start(message):
    text = message.text or ""
    parts = text.split()
    # manage deep link: "/start post_<dbid>"
    if len(parts) > 1 and parts[1].startswith("post_"):
        try:
            dbid = int(parts[1].split("_",1)[1])
        except:
            bot.send_message(message.chat.id, "Неверная ссылка.", reply_markup=main_menu())
            return
        ch = get_channel_by_dbid(dbid)
        if not ch:
            bot.send_message(message.chat.id, "Канал не найден или удалён.", reply_markup=main_menu())
            return
        # offer via deep link: ask anon choice, check cooldown
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Анонимно", callback_data=f"deep_offer_anon:1:{dbid}"),
               types.InlineKeyboardButton("Не анонимно", callback_data=f"deep_offer_anon:0:{dbid}"))
        bot.send_message(message.chat.id, f"📣 Вы хотите отправить пост в канал *{ch[3] or ch[2]}*? Выберите режим отправки:", parse_mode="Markdown", reply_markup=kb)
        return

    pop_state(message.from_user.id)
    bot.send_message(message.chat.id,
                     "Добро пожаловать в Телеформ!\n\nПодключи свой канал, чтобы подписчики могли предлагать посты.👋",
                     reply_markup=main_menu())

@bot.message_handler(commands=["menu"])
def cmd_menu(message):
    pop_state(message.from_user.id)
    bot.send_message(message.chat.id, "Меню:", reply_markup=main_menu())

# ========== MENU HANDLERS ==========
@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("menu_"))
def cq_menu(cq):
    bot.answer_callback_query(cq.id)
    action = cq.data.split("_",1)[1]
    if action == "offer":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Отправить в канал (по ссылке в канале)", callback_data="offer_via_deeplink_info"))
        kb.add(types.InlineKeyboardButton("Отправить в канал (по @username или ссылке)", callback_data="offer_via_username"))
        kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="menu_back"))
        bot.send_message(cq.from_user.id, "Выберите способ отправки:", reply_markup=kb)
    elif action == "channels":
        show_channels_menu(cq.from_user.id)
    elif action == "help":
        # разделённая справка: отправка поста и подключение бота
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✉️ Как отправить пост", callback_data="help_send"))
        kb.add(types.InlineKeyboardButton("🔌 Как подключить бота", callback_data="help_connect"))
        kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="menu_back"))
        bot.send_message(cq.from_user.id,
                         "Выберите тему помощи:",
                         reply_markup=kb)
    elif action == "back":
        bot.send_message(cq.from_user.id, "Возврат в меню.", reply_markup=main_menu())
    else:
        bot.send_message(cq.from_user.id, "Неизвестное действие.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda cq: cq.data == "menu_back")
def cq_menu_back(cq):
    bot.answer_callback_query(cq.id)
    bot.send_message(cq.from_user.id, "Возврат в меню.", reply_markup=main_menu())

# ========== HELP CALLBACKS ==========
@bot.callback_query_handler(func=lambda cq: cq.data == "help_send")
def cq_help_send(cq):
    bot.answer_callback_query(cq.id)
    text = (
        f"✉️ Как отправить пост через Телеформ:\n\n"
        f"1) Через кнопку в канале: владелец канала может отправить сообщение с кнопкой «Предложить пост» — подписчики нажимают и выбирают анонимно/не анонимно.\n\n"
        f"2) Через меню бота: /start → Предложить пост → по @username или ссылке канала.\n\n"
        f"Что можно отправлять: текст (до {MAX_TEXT_LENGTH} символов), фото, видео, документы (макс размер {MAX_FILE_SIZE // (1024*1024)} MB).\n\n"
        f"Важно: действует ограничение по частоте — одна публикация в канал каждые {COOLDOWN_SECONDS//3600} ч. (персональный cooldown).\n\n"
        f"Если заявка отправлена — она попадёт модераторам канала для принятия/отклонения."
    )
    bot.send_message(cq.from_user.id, text)

@bot.callback_query_handler(func=lambda cq: cq.data == "help_connect")
def cq_help_connect(cq):
    bot.answer_callback_query(cq.id)
    text = (
        "🔌 Как подключить бота к каналу — шаги и права:\n\n"
        "1) Добавьте бота в канал как участника.\n"
        "2) Сделайте бота администратором канала (это нужно для публикации сообщений от бота).\n"
        "   Рекомендуемые права: отправлять сообщения, прикреплять медиа/документы. Необязательно: редактировать сообщения.\n\n"
        "3) В личном чате с ботом нажмите «Управление каналами» → «Подключить канал» и перешлите (forward) любое сообщение из вашего канала.\n"
        "   Бот проверит, что вы администратор канала, и сохранит канал в базе.\n\n"
        "4) После подключения можно добавить модераторов, либо владелец будет получать заявки сам.\n\n"
        "Если при подключении возникают ошибки — убедитесь, что вы действительно админ канала и бот имеет права на отправку сообщений."
    )
    bot.send_message(cq.from_user.id, text)

# ========== CHANNEL MANAGEMENT ==========
def show_channels_menu(user_id):
    bot.send_message(user_id, "🔧 Управление каналами:", reply_markup=channels_menu())

@bot.callback_query_handler(func=lambda cq: cq.data == "add_channel")
def cq_add_channel(cq):
    bot.answer_callback_query(cq.id)
    set_state(cq.from_user.id, "wait_channel")
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel"))
    bot.send_message(cq.from_user.id,
                     "📩 Перешли ЛЮБОЕ сообщение из своего канала (Forward)\n\nТы должен быть администратором этого канала.\n\nЕсли хочешь отменить — нажми «Отмена».",
                     reply_markup=kb)

@bot.message_handler(func=lambda m: get_state(m.from_user.id) == "wait_channel", content_types=['text','photo','video','document','sticker'])
def handle_channel_forward(m):
    pop_state(m.from_user.id)
    if not m.forward_from_chat or getattr(m.forward_from_chat, "type", "") != "channel":
        bot.send_message(m.chat.id, "❌ Это не пересылка из канала. Перешли сообщение из своего канала.", reply_markup=main_menu())
        return
    channel = m.forward_from_chat
    channel_id = channel.id
    title = getattr(channel, "title", "") or str(channel_id)
    # проверка прав пользователя в этом канале
    try:
        member = bot.get_chat_member(channel_id, m.from_user.id)
        if member.status not in ("administrator", "creator"):
            bot.send_message(m.chat.id, "❌ Ты не администратор этого канала. Подключение прервано.", reply_markup=main_menu())
            return
    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Не удалось проверить права: {e}\nУбедись, что бот добавлен в канал.", reply_markup=main_menu())
        return
    # attempt to get @username for storage if available
    try:
        info = bot.get_chat(channel_id)
        if getattr(info, 'username', None):
            channel_key = '@' + info.username
        else:
            channel_key = str(channel_id)
    except Exception:
        channel_key = str(channel_id)

    # доп. проверка: если канал уже сохранён (любое представление), сообщаем, что он уже добавлен
    candidate_keys = {channel_key, channel_key.lstrip("@"), str(channel_id)}
    found = None
    for k in candidate_keys:
        cur.execute("SELECT id FROM channels WHERE channel_id = ?", (k,))
        r = cur.fetchone()
        if r:
            found = r[0]
            break
    if found:
        bot.send_message(m.from_user.id, "❗ Канал уже подключён к боту.", reply_markup=channels_menu())
        return

    # сохраняем канал (channel_key может быть @username или numeric id string)
    dbid = add_channel(m.from_user.id, channel_key, title)
    if not dbid:
        bot.send_message(m.from_user.id, "❌ Не удалось сохранить канал (возможно, он уже добавлен).", reply_markup=channels_menu())
        return
    # после добавления — спросим, кто будет получать заявки (модераторы)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Я буду получать заявки", callback_data=f"set_mods_self:{dbid}"))
    kb.add(types.InlineKeyboardButton("Добавить другого модератора", callback_data=f"set_mods_other:{dbid}"))
    kb.add(types.InlineKeyboardButton("Пропустить", callback_data=f"set_mods_skip:{dbid}"))
    # отправляем в канал сообщение с кнопкой "Предложить пост" (deep link)
    bot_link = f"https://t.me/{BOT_USERNAME}?start=post_{dbid}" if BOT_USERNAME else None
    kb_channel = types.InlineKeyboardMarkup()
    if bot_link:
        kb_channel.add(types.InlineKeyboardButton("Предложить пост", url=bot_link))
    try:
        bot.send_message(channel.id, f"Канал подключён к Телеформ — подписчики могут предлагать посты через бота (нажмите кнопку).", reply_markup=kb_channel)
    except Exception:
        # если не получилось отправить в канал — просто продолжим
        pass
    bot.send_message(m.from_user.id, f"✅ Канал *{title}* подключён.\nКто будет получать заявки на модерацию?", parse_mode="Markdown", reply_markup=kb)

# обрабатываем выбор модераторов сразу после подключения
@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("set_mods_"))
def cq_set_mods(cq):
    bot.answer_callback_query(cq.id)
    parts = cq.data.split(":")
    if len(parts) != 2:
        bot.send_message(cq.from_user.id, "Ошибка.")
        return
    cmd, dbid_str = parts[0], parts[1]
    dbid = int(dbid_str)
    if cmd == "set_mods_self":
        # добавляем владельца как модератора
        add_channel_admin(dbid, cq.from_user.id, cq.from_user.id)
        bot.send_message(cq.from_user.id, "👌 Ты добавлен как модератор для этого канала.", reply_markup=channels_menu())
    elif cmd == "set_mods_other":
        # регистрируем состояние ожидания: перешли сообщение или укажи @username/ID
        set_state(cq.from_user.id, f"awaiting_first_mod:{dbid}")
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel"))
        bot.send_message(cq.from_user.id, "Перешли сообщение от пользователя (forward) или отправь @username/ID, чтобы добавить его как модератора.", reply_markup=kb)
    elif cmd == "set_mods_skip":
        bot.send_message(cq.from_user.id, "Ок — модераторы можно добавить позже в меню канала.", reply_markup=channels_menu())
    else:
        bot.send_message(cq.from_user.id, "Неизвестная команда.", reply_markup=channels_menu())

@bot.message_handler(func=lambda m: isinstance(get_state(m.from_user.id), str) and get_state(m.from_user.id).startswith("awaiting_first_mod"), content_types=['text','photo','video','document'])
def handle_first_mod(m):
    state = pop_state(m.from_user.id)
    if not state:
        bot.send_message(m.chat.id, "Сначала начните поток добавления модератора через меню канала.")
        return
    dbid = int(state.split(":",1)[1])
    # определяем кандидата
    admin_candidate = None
    if m.forward_from:
        admin_candidate = m.forward_from.id
    elif m.text and m.text.strip().startswith("@"):
        username = m.text.strip()
        try:
            u = bot.get_chat(username)
            admin_candidate = u.id
        except Exception:
            bot.send_message(m.chat.id, "Не удалось найти пользователя по @username.")
            return
    else:
        try:
            admin_candidate = int(m.text.strip())
        except Exception:
            bot.send_message(m.chat.id, "Неверный ввод. Перешли сообщение от пользователя или отправь @username/ID.")
            return
    res = add_channel_admin(dbid, admin_candidate, m.from_user.id)
    if res:
        bot.send_message(m.chat.id, "✅ Модератор добавлен.", reply_markup=channels_menu())
    else:
        bot.send_message(m.chat.id, "Пользователь уже модератор или произошла ошибка.", reply_markup=channels_menu())

# показать список своих каналов
@bot.callback_query_handler(func=lambda cq: cq.data == "my_channels")
def cq_my_channels(cq):
    bot.answer_callback_query(cq.id)
    rows = list_channels_by_owner(cq.from_user.id)
    if not rows:
        bot.send_message(cq.from_user.id, "📭 У тебя пока нет подключённых каналов.", reply_markup=channels_menu())
        return
    kb = types.InlineKeyboardMarkup()
    for r in rows:
        dbid, channel_id, title = r
        kb.add(types.InlineKeyboardButton(title or str(channel_id), callback_data=f"channel:{dbid}"))
    kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="menu_channels"))
    bot.send_message(cq.from_user.id, "📋 Твои каналы:", reply_markup=kb)

# меню конкретного канала: управление модераторами / удалить / ссылка для подписчиков
@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("channel:"))
def cq_channel(cq):
    bot.answer_callback_query(cq.id)
    dbid = int(cq.data.split(":",1)[1])
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(cq.from_user.id, "Канал не найден.")
        return
    _, owner_id, channel_id, title = ch
    kb = types.InlineKeyboardMarkup()
    bot_link = f"https://t.me/{BOT_USERNAME}?start=post_{dbid}" if BOT_USERNAME else None
    if bot_link:
        kb.add(types.InlineKeyboardButton("🔗 Ссылка для подписчиков", url=bot_link))
    kb.add(types.InlineKeyboardButton("👥 Управление модераторами", callback_data=f"mods:{dbid}"))
    kb.add(types.InlineKeyboardButton("📣 Отправить готовое сообщение в канал", callback_data=f"promo_prepare:{dbid}"))
    kb.add(types.InlineKeyboardButton("🗑 Удалить канал", callback_data=f"delete:{dbid}"))
    kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="my_channels"))
    bot.send_message(cq.from_user.id, f"⚙️ Управление: *{title or channel_id}*", parse_mode="Markdown", reply_markup=kb)

# управление модераторами: список и добавление/удаление
@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("mods:"))
def cq_mods(cq):
    bot.answer_callback_query(cq.id)
    dbid = int(cq.data.split(":",1)[1])
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(cq.from_user.id, "Канал не найден.")
        return
    _, owner_id, channel_id, title = ch
    # показываем список модераторов
    admins = list_channel_admins(dbid)
    text = f"Модераторы канала *{title or channel_id}*:\n"
    if not admins:
        text += "— Нет модераторов —\n"
    else:
        for a in admins:
            try:
                info = bot.get_chat(a)
                name = ("@" + info.username) if getattr(info, "username", None) else (getattr(info, "first_name", "") or str(a))
            except:
                name = str(a)
            text += f"- {name} (ID {a})\n"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Добавить модератора", callback_data=f"addmod:{dbid}"))
    if admins:
        for a in admins:
            kb.add(types.InlineKeyboardButton(f"Удалить {a}", callback_data=f"delmod:{dbid}:{a}"))
    kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data=f"channel:{dbid}"))
    bot.send_message(cq.from_user.id, text, parse_mode="Markdown", reply_markup=kb)

@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("addmod:"))
def cq_addmod(cq):
    bot.answer_callback_query(cq.id)
    dbid = int(cq.data.split(":",1)[1])
    # only owner can add mods
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(cq.from_user.id, "Канал не найден.")
        return
    owner_id = ch[1]
    if cq.from_user.id != owner_id:
        bot.send_message(cq.from_user.id, "Добавлять модераторов может только владелец канала.")
        return
    set_state(cq.from_user.id, f"awaiting_add_mod:{dbid}")
    kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel"))
    bot.send_message(cq.from_user.id, "Перешли сообщение от пользователя (forward) или отправь @username/ID, чтобы добавить модератора.", reply_markup=kb)

@bot.message_handler(func=lambda m: isinstance(get_state(m.from_user.id), str) and get_state(m.from_user.id).startswith("awaiting_add_mod"), content_types=['text','photo','video','document'])
def handle_add_mod(m):
    state = pop_state(m.from_user.id)
    if not state:
        bot.send_message(m.chat.id, "Сначала выбери «Добавить модератора» в меню канала.")
        return
    dbid = int(state.split(":",1)[1])
    admin_candidate = None
    if m.forward_from:
        admin_candidate = m.forward_from.id
    elif m.text and m.text.strip().startswith("@"):
        username = m.text.strip()
        try:
            ch = bot.get_chat(username)
            admin_candidate = ch.id
        except:
            bot.send_message(m.chat.id, "Не удалось найти пользователя по @username.")
            return
    else:
        try:
            admin_candidate = int(m.text.strip())
        except:
            bot.send_message(m.chat.id, "Неверный ввод. Перешли сообщение от пользователя или отправь @username/ID.")
            return
    res = add_channel_admin(dbid, admin_candidate, m.from_user.id)
    if res:
        bot.send_message(m.chat.id, "✅ Модератор добавлен.")
    else:
        bot.send_message(m.chat.id, "Пользователь уже модератор или произошла ошибка.")

@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("delmod:"))
def cq_delmod(cq):
    bot.answer_callback_query(cq.id)
    parts = cq.data.split(":")
    if len(parts) != 3:
        bot.send_message(cq.from_user.id, "Ошибка.")
        return
    dbid = int(parts[1]); admin_id = int(parts[2])
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(cq.from_user.id, "Канал не найден.")
        return
    owner_id = ch[1]
    if cq.from_user.id != owner_id:
        bot.send_message(cq.from_user.id, "Удалять модераторов может только владелец канала.")
        return
    remove_channel_admin(dbid, admin_id)
    bot.send_message(cq.from_user.id, f"Модератор {admin_id} удалён.")

# ========== ADDED: Handler for offer via @username/link ==========
@bot.callback_query_handler(func=lambda cq: cq.data == "offer_via_username")
def cq_offer_via_username(cq):
    bot.answer_callback_query(cq.id)
    set_state(cq.from_user.id, "awaiting_channel_username")
    kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel"))
    bot.send_message(cq.from_user.id, "Отправь @username канала или ссылку на канал (например https://t.me/yourchannel).", reply_markup=kb)

@bot.message_handler(func=lambda m: get_state(m.from_user.id) == "awaiting_channel_username", content_types=['text'])
def handle_channel_by_username(m):
    pop_state(m.from_user.id)
    text = (m.text or "").strip()
    if not text:
        bot.send_message(m.chat.id, "Неверный ввод. Используй @username или ссылку на канал.", reply_markup=main_menu())
        return

    # Сформируем набор кандидатных ключей для поиска в БД
    candidate_keys = set()

    # Прямой ввод ссылки https://t.me/username или http://t.me/username
    if text.startswith("https://t.me/") or text.startswith("http://t.me/"):
        last = text.rstrip("/").split("/")[-1]
        if not last:
            bot.send_message(m.chat.id, "Неверная ссылка.", reply_markup=main_menu())
            return
        # если last — номер (редко), оставим как есть; иначе добавим @
        if last.lstrip("-").isdigit():
            candidate_keys.add(last)
            candidate_keys.add(str(int(last)))  # normalized numeric
        else:
            candidate_keys.add("@" + last)
            candidate_keys.add(last)
    else:
        # если ввели @username или numeric id
        if text.startswith("@"):
            candidate_keys.add(text)
            candidate_keys.add(text.lstrip("@"))
        elif text.lstrip("-").isdigit():
            candidate_keys.add(text)
            candidate_keys.add(str(int(text)))
        else:
            # возможно пользователь ввёл просто username без @
            candidate_keys.add("@" + text)
            candidate_keys.add(text)

    # Попытка 1: прямой поиск в БД по candidate_keys
    row = None
    for k in list(candidate_keys):
        cur.execute("SELECT id, title, channel_id FROM channels WHERE channel_id = ?", (k,))
        r = cur.fetchone()
        if r:
            row = r
            break

    # Попытка 2: если не найдено, попробуем разрешить через bot.get_chat (если есть username/shortname)
    if not row:
        # для get_chat подготовим аргумент: если есть @username — используем, иначе используем last part
        getchat_arg = None
        if text.startswith("https://t.me/") or text.startswith("http://t.me/"):
            last = text.rstrip("/").split("/")[-1]
            if last:
                getchat_arg = "@" + last if not last.lstrip("-").isdigit() and not last.startswith("@") else last
        else:
            if text.startswith("@"):
                getchat_arg = text
            elif text.lstrip("-").isdigit():
                getchat_arg = text
            else:
                getchat_arg = "@" + text

        try:
            chat = bot.get_chat(getchat_arg)
            # возможные варианты ключей, которые могли быть сохранены
            possible = set()
            possible.add(str(chat.id))
            # Bot API возвращает channel username without @
            if getattr(chat, "username", None):
                possible.add("@" + chat.username)
                possible.add(chat.username)
            # иногда id может быть negative like -100..., добавим вариант без -100 если кто-то сохранил так
            cid = str(chat.id)
            if cid.startswith("-100"):
                possible.add(cid[4:])          # without -100
                possible.add(cid.lstrip("-"))  # without minus
            else:
                possible.add("-100" + cid)
                possible.add("-" + cid)

            # поиск в БД по всем возможным вариантам
            for k in possible:
                cur.execute("SELECT id, title, channel_id FROM channels WHERE channel_id = ?", (k,))
                r = cur.fetchone()
                if r:
                    row = r
                    break
        except Exception:
            # если get_chat не удался — продолжаем дальше и сообщим об ошибке позже
            row = None

    if not row:
        bot.send_message(m.chat.id, "❌ Канал не найден или не подключён к боту. Убедитесь, что вы ввели корректный @username или ссылку, и что канал действительно подключён (через Forward).", reply_markup=main_menu())
        return

    dbid, title, stored_key = row
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Анонимно", callback_data=f"deep_offer_anon:1:{dbid}"),
           types.InlineKeyboardButton("Не анонимно", callback_data=f"deep_offer_anon:0:{dbid}"))
    bot.send_message(m.chat.id, f"📣 Вы хотите отправить пост в канал *{title or stored_key}*? Выберите режим отправки:", parse_mode="Markdown", reply_markup=kb)

# ========== DEEP LINK FLOW ==========
@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("deep_offer_anon:"))
def cq_deeplink_offer(cq):
    bot.answer_callback_query(cq.id)
    try:
        _, anon_str, dbid_str = cq.data.split(":",2)
        anon_flag = True if anon_str == "1" else False
        dbid = int(dbid_str)
    except:
        bot.send_message(cq.from_user.id, "Ошибка ссылки."); return
    # cooldown check
    last = get_last_published(cq.from_user.id, dbid)
    if last:
        elapsed = now_ts() - last
        if elapsed < COOLDOWN_SECONDS:
            left = COOLDOWN_SECONDS - elapsed
            bot.send_message(cq.from_user.id, f"⏳ Вы уже публиковали в этот канал. Попробовать ещё можно через {format_timedelta_seconds(left)}.", reply_markup=main_menu())
            return
    # prompt for content
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(cq.from_user.id, "Канал не найден.", reply_markup=main_menu()); return
    msg = bot.send_message(cq.from_user.id, f"📝 Отправьте текст, фото, видео или документ для канала *{ch[3] or ch[2]}*.\nДля отмены нажмите «Отмена».", parse_mode="Markdown", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel")))
    set_state(cq.from_user.id, f"awaiting_submission:{1 if anon_flag else 0}:{dbid}")
    bot.register_next_step_handler(msg, lambda m, anon=anon_flag, target=dbid: handle_submission(m, anon, target))

# ========== HANDLE SUBMISSION ==========
def _reject_submission_from_user(chat_id, reason=""):
    bot.send_message(chat_id, f"❌ Не удалось принять заявку. {reason}", reply_markup=main_menu())

def handle_submission(message, anonymous=True, target_dbid=0):
    uid = message.from_user.id
    st = pop_state(uid)
    if not st or not st.startswith("awaiting_submission"):
        bot.send_message(uid, "Сначала начни через меню: /menu → Предложить пост.", reply_markup=main_menu())
        return
    # извлечь состояние (доп. валидация)
    # content type
    content_type = message.content_type
    text_content = message.text if content_type == 'text' else None
    file_id = None
    file_size = None
    if content_type == 'photo':
        file_id = message.photo[-1].file_id
        try:
            file_size = message.photo[-1].file_size
        except:
            file_size = None
    elif content_type == 'video':
        file_id = message.video.file_id
        file_size = getattr(message.video, 'file_size', None)
    elif content_type == 'document':
        file_id = message.document.file_id
        file_size = getattr(message.document, 'file_size', None)
    elif content_type == 'text':
        file_id = None
    else:
        bot.send_message(uid, "Тип сообщения не поддерживается. Отправь текст, фото, видео или документ.", reply_markup=main_menu())
        return

    # basic validations
    if content_type == 'text' and text_content and len(text_content) > MAX_TEXT_LENGTH:
        _reject_submission_from_user(uid, f"Текст слишком длинный (макс {MAX_TEXT_LENGTH} символов).")
        return
    if file_size and file_size > MAX_FILE_SIZE:
        _reject_submission_from_user(uid, "Файл слишком большой.")
        return

    # require target_dbid > 0 (no "ordinary" submissions allowed)
    if not target_dbid or target_dbid <= 0:
        bot.send_message(uid, "Ошибка: цель публикации не указана. Пожалуйста, отправляйте заявки только в подключённые каналы.", reply_markup=main_menu())
        return

    # recheck cooldown before saving
    last = get_last_published(uid, target_dbid)
    if last and (now_ts() - last) < COOLDOWN_SECONDS:
        left = COOLDOWN_SECONDS - (now_ts() - last)
        bot.send_message(uid, f"⏳ Вы уже публиковали в этот канал. Попробовать ещё можно через {format_timedelta_seconds(left)}.", reply_markup=main_menu())
        return

    # banned check (channel-specific)
    if is_banned(target_dbid, uid):
        _reject_submission_from_user(uid, "Вы заблокированы для этого канала.")
        return

    sub_id = save_submission(uid, content_type, text_content, file_id, anonymous, target_dbid)

    # determine recipients: channel moderators if any, else owner
    recipients = []
    admins = list_channel_admins(target_dbid)
    if admins:
        recipients = admins[:]
    else:
        ch = get_channel_by_dbid(target_dbid)
        if ch:
            recipients = [ch[1]]

    # send submission to each recipient (moderators)
    for r in recipients:
        try:
            if anonymous:
                note = f"Заявка #{sub_id} — анонимно"
                if content_type == 'text':
                    bot.send_message(r, f"{note}\n\n{text_content or ''}")
                elif content_type == 'photo':
                    bot.send_photo(r, file_id, caption=f"{note}\n\n{text_content or ''}")
                elif content_type == 'video':
                    bot.send_video(r, file_id, caption=f"{note}\n\n{text_content or ''}")
                elif content_type == 'document':
                    bot.send_document(r, file_id, caption=f"{note}\n\n{text_content or ''}")
            else:
                bot.forward_message(r, uid, message.message_id)
        except Exception:
            # игнорируем сбои по получателям
            pass
        # send control message with buttons
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Принять", callback_data=f"accept:{sub_id}"),
               types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{sub_id}"))
        kb.add(types.InlineKeyboardButton("✉️ Ответить автору", callback_data=f"reply:{sub_id}"))
        bot.send_message(r, f"🔔 Контроль заявки #{sub_id}", reply_markup=kb)

    bot.send_message(uid, "✅ Ваша заявка отправлена на рассмотрение. Спасибо!", reply_markup=main_menu())

# ========== ADMIN ACTIONS ON SUBMISSIONS (с проверкой прав) ==========
@bot.callback_query_handler(func=lambda cq: cq.data and any(cq.data.startswith(pref) for pref in ("accept:", "reject:", "reply:")))
def cq_admin_submission_actions(cq):
    bot.answer_callback_query(cq.id)
    parts = cq.data.split(":",1)
    action = parts[0]; sid = parts[1]
    try:
        sid = int(sid)
    except:
        bot.send_message(cq.from_user.id, "Неверный ID заявки."); return
    submission = get_submission(sid)
    if not submission:
        bot.send_message(cq.from_user.id, "Заявка не найдена."); return
    sub_id, user_id, content_type, text_content, file_id, status, created_at, anonymous, target_dbid = submission

    # проверка прав: модератор канала или владелец
    if target_dbid and target_dbid > 0:
        ch = get_channel_by_dbid(target_dbid)
        if not ch:
            bot.send_message(cq.from_user.id, "Канал не найден для этой заявки."); return
        owner_id = ch[1]
        admins = list_channel_admins(target_dbid)
        if cq.from_user.id != owner_id and cq.from_user.id not in admins:
            bot.send_message(cq.from_user.id, "У вас нет прав модератора для этой заявки."); return
    else:
        bot.send_message(cq.from_user.id, "Невозможно модерировать заявку без привязки к каналу."); return

    if action == "accept":
        set_submission_status(sub_id, "accepted", moderator_id=cq.from_user.id)
        bot.send_message(cq.from_user.id, f"✅ Заявка #{sub_id} принята.")
        try:
            bot.send_message(user_id, f"✅ Ваша заявка #{sub_id} принята модератором.")
        except:
            pass
        # publish to channel
        if target_dbid and target_dbid > 0:
            handle_publish_to_channel_by_dbid(cq.from_user.id, sub_id, target_dbid)
        return

    if action == "reject":
        set_submission_status(sub_id, "rejected", moderator_id=cq.from_user.id)
        bot.send_message(cq.from_user.id, f"❌ Заявка #{sub_id} отклонена.")
        try:
            bot.send_message(user_id, f"❌ Ваша заявка #{sub_id} отклонена модератором.")
        except:
            pass
        return

    if action == "reply":
        # set state to awaiting reply for this moderator
        set_state(cq.from_user.id, f"awaiting_reply:{sub_id}")
        msg = bot.send_message(cq.from_user.id, f"✍️ Напишите ответ автору заявки #{sub_id} (или нажмите Отмена).", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel")))
        bot.register_next_step_handler(msg, lambda m, sid=sub_id: send_reply_to_author(m, sid))
        return

# ========== PUBLISH TO CHANNEL (с логами и проверками) ==========
def handle_publish_to_channel_by_dbid(requester_id, sub_id, chan_dbid):
    ch = get_channel_by_dbid(chan_dbid)
    if not ch:
        bot.send_message(requester_id, "Канал не найден."); return
    _, owner_id, channel_id, title = ch
    sub = get_submission(sub_id)
    if not sub:
        bot.send_message(requester_id, "Заявка не найдена."); return
    sub_id, user_id, content_type, text_content, file_id, status, created_at, anonymous, target_dbid = sub

    author_str = ""
    if anonymous == 0:
        try:
            info = bot.get_chat(user_id)
            if getattr(info, "username", None):
                author_str = f"Автор: @{info.username}\n\n"
            else:
                name = (getattr(info, "first_name", "") or "") + (" " + getattr(info, "last_name", "") if getattr(info, "last_name", None) else "")
                author_str = f"Автор: {name}\n\n"
        except:
            author_str = ""

    target = channel_id
    try:
        if content_type == 'text':
            bot.send_message(target, author_str + (text_content or ""))
        elif content_type == 'photo':
            bot.send_photo(target, file_id, caption=(author_str + (text_content or "")))
        elif content_type == 'video':
            bot.send_video(target, file_id, caption=(author_str + (text_content or "")))
        elif content_type == 'document':
            bot.send_document(target, file_id, caption=(author_str + (text_content or "")))
        # mark as published
        set_submission_status(sub_id, "published", moderator_id=requester_id)
        # set cooldown for this user-channel
        set_cooldown(user_id, chan_dbid, now_ts())
        # notify requester (moderator) and author
        bot.send_message(requester_id, f"✅ Заявка #{sub_id} опубликована в {title or channel_id}.")
        try:
            bot.send_message(user_id, f"✅ Ваше сообщение #{sub_id} опубликовано в канал *{title or channel_id}*.", parse_mode="Markdown")
        except:
            pass
    except Exception as e:
        bot.send_message(requester_id, f"Ошибка при публикации: {e}\nУбедитесь, что бот админ в канале и имеет права на отправку сообщений.")

# ========== SEND REPLY TO AUTHOR ==========
def send_reply_to_author(message, sub_id):
    state = pop_state(message.from_user.id)
    try:
        sub = get_submission(sub_id)
        if not sub:
            bot.send_message(message.from_user.id, "Заявка не найдена."); return
        user_id = sub[1]
        bot.send_message(user_id, f"✉️ Ответ модератора по заявке #{sub_id}:\n\n{message.text}")
        bot.send_message(message.from_user.id, "Ответ отправлен.")
        # логируем действие reply
        try:
            cur.execute("INSERT INTO submission_actions (submission_id, moderator_id, action, note, created_at) VALUES (?, ?, ?, ?, ?)", (sub_id, message.from_user.id, 'reply', message.text or '', now_ts()))
            db.commit()
        except Exception:
            pass
    except Exception:
        bot.send_message(message.from_user.id, "Не удалось отправить ответ (возможно, пользователь закрыл диалог).")

# ========== PROMO PREPARE (owner posts a ready message with bot link) ==========
@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("promo_prepare:"))
def cq_promo_prepare(cq):
    bot.answer_callback_query(cq.id)
    dbid = int(cq.data.split(":",1)[1])
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(cq.from_user.id, "Канал не найден."); return
    _, owner_id, channel_id, title = ch
    if cq.from_user.id != owner_id:
        bot.send_message(cq.from_user.id, "Эту операцию может выполнять только владелец канала."); return
    bot_link = f"https://t.me/{BOT_USERNAME}?start=post_{dbid}" if BOT_USERNAME else None
    text = f"📣 Хотите отправить пост в канал *{title or channel_id}*? Нажмите кнопку и предложите пост через бота — он попадёт на модерацию."
    kb = types.InlineKeyboardMarkup()
    if bot_link:
        kb.add(types.InlineKeyboardButton("Предложить пост", url=bot_link))
    try:
        # try to send using numeric id or username
        try:
            bot.send_message(channel_id, text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            # maybe stored channel_id is @username, resolve and send
            if str(channel_id).startswith('@'):
                try:
                    bot.send_message(channel_id, text, parse_mode="Markdown", reply_markup=kb)
                except Exception as e:
                    raise e
        bot.send_message(cq.from_user.id, "Готовое сообщение отправлено в канал.", reply_markup=channels_menu())
    except Exception as e:
        bot.send_message(cq.from_user.id, f"Ошибка при отправке в канал: {e}", reply_markup=channels_menu())

# ========== DELETE CHANNEL ==========
@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("delete:"))
def cq_delete(cq):
    bot.answer_callback_query(cq.id)
    dbid = int(cq.data.split(":",1)[1])
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(cq.from_user.id, "Канал не найден."); return
    _, owner_id, _, title = ch
    if cq.from_user.id != owner_id:
        bot.send_message(cq.from_user.id, "Удалять канал может только его владелец."); return
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"delete_yes:{dbid}"),
           types.InlineKeyboardButton("❌ Отмена", callback_data="my_channels"))
    bot.send_message(cq.from_user.id, f"Вы действительно хотите удалить канал *{title or ''}*?", parse_mode="Markdown", reply_markup=kb)

@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("delete_yes:"))
def cq_delete_yes(cq):
    bot.answer_callback_query(cq.id)
    dbid = int(cq.data.split(":",1)[1])
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(cq.from_user.id, "Канал не найден."); return
    if cq.from_user.id != ch[1]:
        bot.send_message(cq.from_user.id, "Удалять канал может только владелец."); return
    remove_channel(dbid)
    bot.send_message(cq.from_user.id, "Канал удалён.", reply_markup=main_menu())

# ========== Бан пользователя для канала (owner только) ==========
@bot.message_handler(commands=['ban'])
def cmd_ban(message):
    # формат: /ban <channel_dbid> <user_id>
    parts = (message.text or "").split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "Использование: /ban <channel_dbid> <user_id>")
        return
    try:
        dbid = int(parts[1]); uid = int(parts[2])
    except:
        bot.send_message(message.chat.id, "Неверные аргументы.")
        return
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(message.chat.id, "Канал не найден.")
        return
    if message.from_user.id != ch[1]:
        bot.send_message(message.chat.id, "Только владелец канала может банить пользователей.")
        return
    res = add_ban(dbid, uid, message.from_user.id)
    bot.send_message(message.chat.id, "Пользователь заблокирован для этого канала." if res else "Пользователь уже заблокирован.")

@bot.message_handler(commands=['unban'])
def cmd_unban(message):
    # формат: /unban <channel_dbid> <user_id>
    parts = (message.text or "").split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "Использование: /unban <channel_dbid> <user_id>")
        return
    try:
        dbid = int(parts[1]); uid = int(parts[2])
    except:
        bot.send_message(message.chat.id, "Неверные аргументы.")
        return
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(message.chat.id, "Канал не найден.")
        return
    if message.from_user.id != ch[1]:
        bot.send_message(message.chat.id, "Только владелец канала может снимать блокировку.")
        return
    remove_ban(dbid, uid)
    bot.send_message(message.chat.id, "Пользователь разблокирован.")

# ========== UNIVERSAL CANCEL ==========
@bot.callback_query_handler(func=lambda cq: cq.data == "cancel")
def cq_cancel(cq):
    bot.answer_callback_query(cq.id, "Действие отменено.")
    pop_state(cq.from_user.id)
    bot.send_message(cq.from_user.id, "Действие отменено.", reply_markup=main_menu())

# ========== UNEXPECTED INPUT HANDLER (when in state) ==========
@bot.message_handler(func=lambda m: get_state(m.from_user.id) is not None)
def handle_unexpected_input(m):
    st = get_state(m.from_user.id)
    bot.send_message(m.chat.id, "Я сейчас ожидаю конкретные данные — либо отправь их, либо нажми «Отмена». Для возврата в меню напиши /menu", reply_markup=types.ReplyKeyboardRemove())

# ========== DEFAULT PRIVATE MESSAGE HANDLER ==========
@bot.message_handler(func=lambda m: (m.chat.type == 'private') and (get_state(m.from_user.id) is None) and (m.text is not None) and (not m.text.startswith('/')) , content_types=['text'])
def handle_private_default(m):
    bot.send_message(m.chat.id, "Чтобы войти в меню напишите /start")

# ========== Показать pending заявки для модератора ==========
@bot.message_handler(commands=['pending'])
def cmd_pending(message):
    uid = message.from_user.id
    # найдем все каналы, где пользователь модератор или владелец
    cur.execute("SELECT channel_dbid FROM channel_admins WHERE admin_user_id = ?", (uid,))
    admin_rows = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT id FROM channels WHERE owner_id = ?", (uid,))
    owner_rows = [r[0] for r in cur.fetchall()]
    watch_dbids = set(admin_rows + owner_rows)
    if not watch_dbids:
        bot.send_message(uid, "Вы не модератор и не владелец ни одного канала.")
        return
    # получить pending заявки для этих каналов
    placeholders = ','.join('?' for _ in watch_dbids)
    query = f"SELECT id, user_id, content_type, text_content, file_id, created_at, anonymous, target_channel_dbid FROM submissions WHERE status = 'pending' AND target_channel_dbid IN ({placeholders}) ORDER BY created_at DESC"
    cur.execute(query, tuple(watch_dbids))
    rows = cur.fetchall()
    if not rows:
        bot.send_message(uid, "Нет ожидающих заявок.")
        return
    for r in rows[:20]:  # ограничим вывод
        sid, user_id, ctype, txt, fid, created_at, anon, tdb = r
        title = f"Заявка #{sid} — {'анонимно' if anon else 'неанонимно'} — канал {tdb}"
        if ctype == 'text':
            bot.send_message(uid, f"{title}\n\n{(txt or '')[:1000]}", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("✅ Принять", callback_data=f"accept:{sid}"), types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{sid}"), types.InlineKeyboardButton("✉️ Ответить автору", callback_data=f"reply:{sid}")))
        else:
            bot.send_message(uid, f"{title}\nТип: {ctype}\nID файла: {fid}", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("✅ Принять", callback_data=f"accept:{sid}"), types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{sid}"), types.InlineKeyboardButton("✉️ Ответить автору", callback_data=f"reply:{sid}")))

# ========== WEBHOOK: Flask-приложение для Telegram ==========
app = Flask(__name__)

# health check
@app.route("/", methods=["GET"])
def index():
    return "OK", 200

WEBHOOK_PATH = f"/webhook/{TOKEN}"

@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") == "application/json":
        try:
            json_string = request.get_data().decode("utf-8")
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
        except Exception as e:
            logger.exception("Failed to process update: %s", e)
            return "", 500
        return "", 200
    else:
        return abort(403)

def setup_webhook():
    webhook_url = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH
    try:
        logger.info("Removing old webhook (if any)...")
        bot.remove_webhook()
    except Exception:
        pass
    try:
        logger.info("Setting webhook to: %s", webhook_url)
        ok = bot.set_webhook(url=webhook_url)
        if not ok:
            logger.error("set_webhook returned False")
        else:
            logger.info("Webhook установлен успешно")
    except Exception as e:
        logger.exception("Не удалось установить webhook: %s", e)
        raise

# Попытка установки webhook при импорте (gunicorn будет импортировать модуль)
try:
    setup_webhook()
except Exception as e:
    logger.error("Ошибка при установке webhook: %s", e)

# ========== Запуск приложения (локально) ==========
if __name__ == "__main__":
    logger.info("Запуск Flask (local) на 0.0.0.0:%s", PORT)
    app.run(host="0.0.0.0", port=PORT)
