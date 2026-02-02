#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Телеграм-бот (один файл).
Изменения: реализован COOLDOWN 1 час при отправке заявки (сохранение в БД).
Сохранение кд в таблице cooldowns — кд не теряется при перезапуске.

Установка зависимостей:
pip install pyTelegramBotAPI Flask psycopg2-binary
(если не используете Postgres, достаточно sqlite3 который в stdlib)

Настройка:
- Замените TOKEN на токен бота.
- При желании измените COOLDOWN_SECONDS (по умолчанию 3600 — 1 час).
"""

import os
import time
import logging
import sqlite3
from datetime import timedelta
from flask import Flask, request, abort

import telebot
from telebot import types

# --------------- CONFIG ----------------
TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
WEBHOOK_BASE = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL", "https://your-service.onrender.com")
PORT = int(os.environ.get("PORT", 5000))
COOLDOWN_SECONDS = 3600  # 1 час (измените при необходимости)
MAX_TEXT_LENGTH = 4000
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
DB_PATH = os.environ.get("DB_PATH", "teleform_cd.db")
# ---------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== DATABASE (SQLite fallback) ==========
# Структура таблиц: channels, channel_admins, submissions, cooldowns, user_states, bans, submission_actions
db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
cur = db.cursor()

# Create tables if not exist (idempotent)
cur.execute('''
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER,
    channel_id TEXT UNIQUE,
    title TEXT,
    created_at INTEGER
)
''')
cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_channel_id ON channels(channel_id)')

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

cur.execute('''
CREATE TABLE IF NOT EXISTS cooldowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    channel_dbid INTEGER,
    last_ts INTEGER,
    UNIQUE(user_id, channel_dbid)
)
''')

cur.execute('''
CREATE TABLE IF NOT EXISTS user_states (
    user_id INTEGER PRIMARY KEY,
    state TEXT,
    updated_at INTEGER
)
''')

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

# ========== BOT ==========
bot = telebot.TeleBot(TOKEN)
try:
    BOT_USERNAME = bot.get_me().username
except Exception:
    BOT_USERNAME = None

# ========== HELPERS ==========
def now_ts():
    return int(time.time())

def set_state(user_id, state):
    ts = now_ts()
    cur.execute("INSERT OR REPLACE INTO user_states (user_id, state, updated_at) VALUES (?, ?, ?)", (user_id, state, ts))
    db.commit()

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
    try:
        cur.execute("INSERT INTO channels (owner_id, channel_id, title, created_at) VALUES (?, ?, ?, ?)", (owner_id, str(channel_id), title, ts))
        db.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        cur.execute("SELECT id FROM channels WHERE channel_id = ?", (str(channel_id),))
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None

def get_channel_by_dbid(dbid):
    cur.execute("SELECT id, owner_id, channel_id, title FROM channels WHERE id = ?", (dbid,))
    return cur.fetchone()

def list_channel_admins(channel_dbid):
    cur.execute("SELECT admin_user_id FROM channel_admins WHERE channel_dbid = ?", (channel_dbid,))
    return [r[0] for r in cur.fetchall()]

def add_channel_admin(channel_dbid, admin_user_id, added_by):
    ts = now_ts()
    try:
        cur.execute("INSERT INTO channel_admins (channel_dbid, admin_user_id, added_by, created_at) VALUES (?, ?, ?, ?)", (channel_dbid, admin_user_id, added_by, ts))
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False

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
        cur.execute("INSERT INTO submission_actions (submission_id, moderator_id, action, note, created_at) VALUES (?, ?, ?, ?, ?)", (sub_id, moderator_id, status, note or "", ts))
    db.commit()

# cooldowns (persistent)
def set_cooldown(user_id, channel_dbid, ts=None):
    ts = ts or now_ts()
    try:
        cur.execute("INSERT INTO cooldowns (user_id, channel_dbid, last_ts) VALUES (?, ?, ?)", (user_id, channel_dbid, ts))
    except sqlite3.IntegrityError:
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

def is_banned(channel_dbid, user_id):
    cur.execute("SELECT 1 FROM bans WHERE channel_dbid = ? AND user_id = ?", (channel_dbid, user_id))
    return bool(cur.fetchone())

# utils
def format_timedelta_seconds(sec):
    if sec <= 0:
        return "0:00:00"
    td = timedelta(seconds=sec)
    hours = td.seconds // 3600 + td.days * 24
    minutes = (td.seconds % 3600) // 60
    seconds = td.seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

# --------------- MARKUPS ----------------
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

# --------------- HANDLERS ----------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    pop_state(message.from_user.id)
    bot.send_message(message.chat.id,
                     "Добро пожаловать! Отправляйте предложения через меню.\n\nКД: 1 сообщение в час для одного канала.",
                     reply_markup=main_menu())

@bot.message_handler(commands=["menu"])
def cmd_menu(message):
    pop_state(message.from_user.id)
    bot.send_message(message.chat.id, "Меню:", reply_markup=main_menu())

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
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✉️ Как отправить пост", callback_data="help_send"))
        kb.add(types.InlineKeyboardButton("🔌 Как подключить бота", callback_data="help_connect"))
        kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="menu_back"))
        bot.send_message(cq.from_user.id, "Выберите тему помощи:", reply_markup=kb)
    else:
        bot.send_message(cq.from_user.id, "Неизвестное действие.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda cq: cq.data == "menu_back")
def cq_menu_back(cq):
    bot.answer_callback_query(cq.id)
    bot.send_message(cq.from_user.id, "Возврат в меню.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda cq: cq.data == "help_send")
def cq_help_send(cq):
    bot.answer_callback_query(cq.id)
    text = (
        "✉️ Как отправить пост:\n\n"
        "1) Через кнопку в канале (owner подключает канал)\n"
        "2) Через меню бота: Предложить пост → указать канал по @username или по deep link\n\n"
        f"Ограничение по частоте: одна публикация/отправка заявки для одного канала — каждые {COOLDOWN_SECONDS//3600} ч."
    )
    bot.send_message(cq.from_user.id, text)

@bot.callback_query_handler(func=lambda cq: cq.data == "help_connect")
def cq_help_connect(cq):
    bot.answer_callback_query(cq.id)
    text = (
        "🔌 Как подключить бота к каналу:\n\n"
        "1) Добавьте бота в канал\n"
        "2) Сделайте бота админом (права на отправку сообщений)\n"
        "3) В личном чате с ботом → Управление каналами → Подключить канал (перешлите любое сообщение из канала)"
    )
    bot.send_message(cq.from_user.id, text)

def show_channels_menu(user_id):
    bot.send_message(user_id, "🔧 Управление каналами:", reply_markup=channels_menu())

@bot.callback_query_handler(func=lambda cq: cq.data == "add_channel")
def cq_add_channel(cq):
    bot.answer_callback_query(cq.id)
    set_state(cq.from_user.id, "wait_channel")
    kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel"))
    bot.send_message(cq.from_user.id, "Перешли любое сообщение из своего канала (Forward).", reply_markup=kb)

@bot.message_handler(func=lambda m: get_state(m.from_user.id) == "wait_channel", content_types=['text','photo','video','document','sticker'])
def handle_channel_forward(m):
    pop_state(m.from_user.id)
    if not m.forward_from_chat or getattr(m.forward_from_chat, "type", "") != "channel":
        bot.send_message(m.chat.id, "Это не пересылка из канала. Перешли сообщение из своего канала.", reply_markup=main_menu())
        return
    channel = m.forward_from_chat
    channel_id = channel.id
    title = getattr(channel, "title", "") or str(channel_id)
    try:
        member = bot.get_chat_member(channel_id, m.from_user.id)
        if member.status not in ("administrator", "creator"):
            bot.send_message(m.chat.id, "Ты не администратор этого канала.", reply_markup=main_menu())
            return
    except Exception as e:
        bot.send_message(m.chat.id, f"Не удалось проверить права: {e}", reply_markup=main_menu())
        return

    try:
        info = bot.get_chat(channel_id)
        channel_key = '@' + info.username if getattr(info, 'username', None) else str(channel_id)
    except Exception:
        channel_key = str(channel_id)

    dbid = add_channel(m.from_user.id, channel_key, title)
    if not dbid:
        bot.send_message(m.from_user.id, "Не удалось сохранить канал.", reply_markup=channels_menu())
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Я буду получать заявки", callback_data=f"set_mods_self:{dbid}"),
           types.InlineKeyboardButton("Пропустить", callback_data=f"set_mods_skip:{dbid}"))
    bot.send_message(m.from_user.id, f"Канал {title} подключён.", reply_markup=kb)

@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("set_mods_"))
def cq_set_mods(cq):
    bot.answer_callback_query(cq.id)
    parts = cq.data.split(":")
    cmd = parts[0]
    dbid = int(parts[1])
    if cmd == "set_mods_self":
        add_channel_admin(dbid, cq.from_user.id, cq.from_user.id)
        bot.send_message(cq.from_user.id, "Ты добавлен как модератор.", reply_markup=channels_menu())
    else:
        bot.send_message(cq.from_user.id, "Пропущено.", reply_markup=channels_menu())

@bot.callback_query_handler(func=lambda cq: cq.data == "my_channels")
def cq_my_channels(cq):
    bot.answer_callback_query(cq.id)
    cur.execute("SELECT id, channel_id, title FROM channels WHERE owner_id = ? ORDER BY created_at DESC", (cq.from_user.id,))
    rows = cur.fetchall()
    if not rows:
        bot.send_message(cq.from_user.id, "У тебя пока нет подключённых каналов.", reply_markup=channels_menu())
        return
    kb = types.InlineKeyboardMarkup()
    for r in rows:
        dbid, channel_key, title = r
        kb.add(types.InlineKeyboardButton(title or str(channel_key), callback_data=f"channel:{dbid}"))
    kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="menu_channels"))
    bot.send_message(cq.from_user.id, "Твои каналы:", reply_markup=kb)

@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("channel:"))
def cq_channel(cq):
    bot.answer_callback_query(cq.id)
    dbid = int(cq.data.split(":",1)[1])
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(cq.from_user.id, "Канал не найден.")
        return
    _, owner_id, channel_key, title = ch
    kb = types.InlineKeyboardMarkup()
    if BOT_USERNAME:
        bot_link = f"https://t.me/{BOT_USERNAME}?start=post_{dbid}"
        kb.add(types.InlineKeyboardButton("🔗 Ссылка для подписчиков", url=bot_link))
    kb.add(types.InlineKeyboardButton("👥 Управление модераторами", callback_data=f"mods:{dbid}"))
    kb.add(types.InlineKeyboardButton("🗑 Удалить канал", callback_data=f"delete:{dbid}"))
    kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="my_channels"))
    bot.send_message(cq.from_user.id, f"Управление: {title or channel_key}", reply_markup=kb)

@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("mods:"))
def cq_mods(cq):
    bot.answer_callback_query(cq.id)
    dbid = int(cq.data.split(":",1)[1])
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(cq.from_user.id, "Канал не найден.")
        return
    _, owner_id, channel_key, title = ch
    admins = list_channel_admins(dbid)
    text = f"Модераторы канала {title or channel_key}:\n"
    if not admins:
        text += "— Нет модераторов —\n"
    else:
        for a in admins:
            text += f"- {a}\n"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Добавить модератора", callback_data=f"addmod:{dbid}"))
    if admins:
        for a in admins:
            kb.add(types.InlineKeyboardButton(f"Удалить {a}", callback_data=f"delmod:{dbid}:{a}"))
    kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data=f"channel:{dbid}"))
    bot.send_message(cq.from_user.id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("addmod:"))
def cq_addmod(cq):
    bot.answer_callback_query(cq.id)
    dbid = int(cq.data.split(":",1)[1])
    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(cq.from_user.id, "Канал не найден.")
        return
    if cq.from_user.id != ch[1]:
        bot.send_message(cq.from_user.id, "Добавлять модераторов может только владелец канала.")
        return
    set_state(cq.from_user.id, f"awaiting_add_mod:{dbid}")
    bot.send_message(cq.from_user.id, "Перешли сообщение от пользователя (forward) или отправь @username/ID, чтобы добавить модератора.")

@bot.message_handler(func=lambda m: isinstance(get_state(m.from_user.id), str) and get_state(m.from_user.id).startswith("awaiting_add_mod"), content_types=['text','photo','video','document'])
def handle_add_mod(m):
    state = pop_state(m.from_user.id)
    if not state:
        bot.send_message(m.chat.id, "Ошибка состояния.")
        return
    dbid = int(state.split(":",1)[1])
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
            bot.send_message(m.chat.id, "Неверный ввод.")
            return
    res = add_channel_admin(dbid, admin_candidate, m.from_user.id)
    bot.send_message(m.chat.id, "Модератор добавлен." if res else "Ошибка или уже модератор.")

# Offer via username flow
@bot.callback_query_handler(func=lambda cq: cq.data == "offer_via_username")
def cq_offer_via_username(cq):
    bot.answer_callback_query(cq.id)
    set_state(cq.from_user.id, "awaiting_channel_username")
    bot.send_message(cq.from_user.id, "Отправь @username или ссылку на канал (например https://t.me/yourchannel).")

@bot.message_handler(func=lambda m: get_state(m.from_user.id) == "awaiting_channel_username", content_types=['text'])
def handle_channel_by_username(m):
    pop_state(m.from_user.id)
    text = (m.text or "").strip()
    if not text:
        bot.send_message(m.chat.id, "Неверный ввод.", reply_markup=main_menu())
        return

    candidate_keys = set()
    if text.startswith("https://t.me/") or text.startswith("http://t.me/"):
        last = text.rstrip("/").split("/")[-1]
        if not last:
            bot.send_message(m.chat.id, "Неверная ссылка.", reply_markup=main_menu())
            return
        if last.lstrip("-").isdigit():
            candidate_keys.add(last)
            candidate_keys.add(str(int(last)))
        else:
            candidate_keys.add("@" + last)
            candidate_keys.add(last)
    else:
        if text.startswith("@"):
            candidate_keys.add(text)
            candidate_keys.add(text.lstrip("@"))
        elif text.lstrip("-").isdigit():
            candidate_keys.add(text)
            candidate_keys.add(str(int(text)))
        else:
            candidate_keys.add("@" + text)
            candidate_keys.add(text)

    row = None
    for k in list(candidate_keys):
        cur.execute("SELECT id, title, channel_id FROM channels WHERE channel_id = ?", (k,))
        r = cur.fetchone()
        if r:
            row = r
            break

    if not row:
        # Try to resolve via get_chat
        try:
            if text.startswith("https://") or text.startswith("http://"):
                last = text.rstrip("/").split("/")[-1]
                get_arg = "@" + last if not last.lstrip("-").isdigit() else last
            else:
                if text.startswith("@") or text.lstrip("-").isdigit():
                    get_arg = text
                else:
                    get_arg = "@" + text
            chat = bot.get_chat(get_arg)
            possible = set()
            possible.add(str(chat.id))
            if getattr(chat, "username", None):
                possible.add("@" + chat.username)
                possible.add(chat.username)
            for k in possible:
                cur.execute("SELECT id, title, channel_id FROM channels WHERE channel_id = ?", (k,))
                r = cur.fetchone()
                if r:
                    row = r
                    break
        except Exception:
            row = None

    if not row:
        bot.send_message(m.chat.id, "Канал не найден или не подключён.", reply_markup=main_menu())
        return

    dbid, title, stored_key = row
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Анонимно", callback_data=f"deep_offer_anon:1:{dbid}"),
           types.InlineKeyboardButton("Не анонимно", callback_data=f"deep_offer_anon:0:{dbid}"))
    bot.send_message(m.chat.id, f"Вы хотите отправить пост в канал {title or stored_key}? Выберите:", reply_markup=kb)

# Deep link flow (start=post_dbid)
@bot.callback_query_handler(func=lambda cq: cq.data and cq.data.startswith("deep_offer_anon:"))
def cq_deeplink_offer(cq):
    bot.answer_callback_query(cq.id)
    try:
        _, anon_str, dbid_str = cq.data.split(":",2)
        anon_flag = True if anon_str == "1" else False
        dbid = int(dbid_str)
    except:
        bot.send_message(cq.from_user.id, "Ошибка ссылки.")
        return

    # Check cooldown BEFORE prompting for content
    last = get_last_published(cq.from_user.id, dbid)
    if last and (now_ts() - last) < COOLDOWN_SECONDS:
        left = COOLDOWN_SECONDS - (now_ts() - last)
        bot.send_message(cq.from_user.id, f"⏳ Вы уже отправляли заявку в этот канал. Попробовать можно через {format_timedelta_seconds(left)}.", reply_markup=main_menu())
        return

    ch = get_channel_by_dbid(dbid)
    if not ch:
        bot.send_message(cq.from_user.id, "Канал не найден.", reply_markup=main_menu())
        return
    msg = bot.send_message(cq.from_user.id, f"Отправьте текст, фото, видео или документ для канала {ch[3] or ch[2]}. Для отмены /cancel.")
    set_state(cq.from_user.id, f"awaiting_submission:{1 if anon_flag else 0}:{dbid}")
    bot.register_next_step_handler(msg, lambda m, anon=anon_flag, target=dbid: handle_submission(m, anon, target))

@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    uid = message.from_user.id
    popped = False
    if get_state(uid):
        pop_state(uid)
        bot.reply_to(message, "Действие отменено.")
        popped = True
    if not popped:
        bot.reply_to(message, "Нечего отменять.")

def _reject_submission_from_user(chat_id, reason=""):
    bot.send_message(chat_id, f"❌ Не удалось принять заявку. {reason}", reply_markup=main_menu())

# Core: handle_submission
def handle_submission(message, anonymous=True, target_dbid=0):
    uid = message.from_user.id
    st = pop_state(uid)
    if not st or not st.startswith("awaiting_submission"):
        bot.send_message(uid, "Сначала начните через меню.", reply_markup=main_menu())
        return

    # Validate content
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
        bot.send_message(uid, "Тип сообщения не поддерживается.", reply_markup=main_menu())
        return

    if content_type == 'text' and text_content and len(text_content) > MAX_TEXT_LENGTH:
        _reject_submission_from_user(uid, f"Текст слишком длинный (макс {MAX_TEXT_LENGTH}).")
        return
    if file_size and file_size > MAX_FILE_SIZE:
        _reject_submission_from_user(uid, "Файл слишком большой.")
        return

    if not target_dbid or target_dbid <= 0:
        bot.send_message(uid, "Ошибка: цель публикации не указана.", reply_markup=main_menu())
        return

    # Recheck cooldown (race conditions)
    last = get_last_published(uid, target_dbid)
    if last and (now_ts() - last) < COOLDOWN_SECONDS:
        left = COOLDOWN_SECONDS - (now_ts() - last)
        bot.send_message(uid, f"⏳ Вы уже отправляли заявку в этот канал. Попробовать можно через {format_timedelta_seconds(left)}.", reply_markup=main_menu())
        return

    # Banned check
    if is_banned(target_dbid, uid):
        _reject_submission_from_user(uid, "Вы заблокированы для этого канала.")
        return

    # Save submission
    sub_id = save_submission(uid, content_type, text_content, file_id, anonymous, target_dbid)

    # IMPORTANT: set cooldown at submission time to prevent immediate spamming to moderators.
    # This is the key change: cooldown persists in DB and will block new submissions for the same channel for COOLDOWN_SECONDS.
    try:
        set_cooldown(uid, target_dbid, now_ts())
    except Exception:
        # non-fatal: proceed but note that cooldown might not be stored
        logger.exception("Не удалось установить cooldown в БД")

    # Determine recipients (moderators or owner)
    admins = list_channel_admins(target_dbid)
    recipients = admins[:] if admins else []
    if not recipients:
        ch = get_channel_by_dbid(target_dbid)
        if ch:
            recipients = [ch[1]]

    # Send submission to recipients (moderators/owner)
    for r in recipients:
        try:
            if anonymous:
                note = f"Заявка #{sub_id} — анонимно"
                if content_type == 'text':
                    bot.send_message(r, f"{note}\n\n{(text_content or '')}")
                elif content_type == 'photo':
                    bot.send_photo(r, file_id, caption=f"{note}\n\n{(text_content or '')}")
                elif content_type == 'video':
                    bot.send_video(r, file_id, caption=f"{note}\n\n{(text_content or '')}")
                elif content_type == 'document':
                    bot.send_document(r, file_id, caption=f"{note}\n\n{(text_content or '')}")
            else:
                bot.forward_message(r, uid, message.message_id)
        except Exception:
            logger.exception("Ошибка при отправке заявки получателю %s", r)
        # control buttons (accept / reject / reply)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Принять", callback_data=f"accept:{sub_id}"),
               types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{sub_id}"))
        kb.add(types.InlineKeyboardButton("✉️ Ответить автору", callback_data=f"reply:{sub_id}"))
        try:
            bot.send_message(r, f"🔔 Контроль заявки #{sub_id}", reply_markup=kb)
        except Exception:
            logger.exception("Не удалось отправить контрольное сообщение получателю %s", r)

    bot.send_message(uid, "✅ Ваша заявка отправлена на рассмотрение. Спасибо!", reply_markup=main_menu())

# Moderator actions: accept/reject/reply (uses existing channel_admins/owner checks)
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

    # rights check: owner or channel admin
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
        # publish immediately to channel and mark published (this function below handles cooldown on publish as well)
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
        set_state(cq.from_user.id, f"awaiting_reply:{sub_id}")
        msg = bot.send_message(cq.from_user.id, f"Напишите ответ автору заявки #{sub_id} (или /cancel).")
        bot.register_next_step_handler(msg, lambda m, sid=sub_id: send_reply_to_author(m, sid))
        return

def handle_publish_to_channel_by_dbid(requester_id, sub_id, chan_dbid):
    ch = get_channel_by_dbid(chan_dbid)
    if not ch:
        bot.send_message(requester_id, "Канал не найден."); return
    _, owner_id, channel_key, title = ch
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

    target = channel_key
    try:
        if content_type == 'text':
            bot.send_message(target, author_str + (text_content or ""))
        elif content_type == 'photo':
            bot.send_photo(target, file_id, caption=(author_str + (text_content or "")))
        elif content_type == 'video':
            bot.send_video(target, file_id, caption=(author_str + (text_content or "")))
        elif content_type == 'document':
            bot.send_document(target, file_id, caption=(author_str + (text_content or "")))
        set_submission_status(sub_id, "published", moderator_id=requester_id)
        # Ensure cooldown is set at publish as well (redundant but safe)
        try:
            set_cooldown(user_id, chan_dbid, now_ts())
        except Exception:
            logger.exception("Не удалось обновить cooldown при публикации")
        bot.send_message(requester_id, f"✅ Заявка #{sub_id} опубликована в {title or channel_key}.")
        try:
            bot.send_message(user_id, f"✅ Ваше сообщение #{sub_id} опубликовано в канал {title or channel_key}.")
        except:
            pass
    except Exception as e:
        bot.send_message(requester_id, f"Ошибка при публикации: {e}\nУбедитесь, что бот админ в канале и имеет права.")

def send_reply_to_author(message, sub_id):
    state = pop_state(message.from_user.id)
    try:
        sub = get_submission(sub_id)
        if not sub:
            bot.send_message(message.from_user.id, "Заявка не найдена."); return
        user_id = sub[1]
        bot.send_message(user_id, f"Ответ модератора по заявке #{sub_id}:\n\n{message.text}")
        bot.send_message(message.from_user.id, "Ответ отправлен.")
        # log action
        ts = now_ts()
        cur.execute("INSERT INTO submission_actions (submission_id, moderator_id, action, note, created_at) VALUES (?, ?, ?, ?, ?)", (sub_id, message.from_user.id, 'reply', message.text or '', ts))
        db.commit()
    except Exception:
        bot.send_message(message.from_user.id, "Не удалось отправить ответ (возможно пользователь закрыл диалог).")

@bot.message_handler(commands=['pending'])
def cmd_pending(message):
    uid = message.from_user.id
    cur.execute("SELECT channel_dbid FROM channel_admins WHERE admin_user_id = ?", (uid,))
    admin_rows = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT id FROM channels WHERE owner_id = ?", (uid,))
    owner_rows = [r[0] for r in cur.fetchall()]
    watch_dbids = set(admin_rows + owner_rows)
    if not watch_dbids:
        bot.send_message(uid, "Вы не модератор и не владелец ни одного канала.")
        return
    placeholders = ','.join('?' for _ in watch_dbids)
    query = f"SELECT id, user_id, content_type, text_content, file_id, created_at, anonymous, target_channel_dbid FROM submissions WHERE status = 'pending' AND target_channel_dbid IN ({placeholders}) ORDER BY created_at DESC"
    cur.execute(query, tuple(watch_dbids))
    rows = cur.fetchall()
    if not rows:
        bot.send_message(uid, "Нет ожидающих заявок.")
        return
    for r in rows[:20]:
        sid, user_id, ctype, txt, fid, created_at, anon, tdb = r
        title = f"Заявка #{sid} — {'анонимно' if anon else 'неанонимно'} — канал {tdb}"
        if ctype == 'text':
            bot.send_message(uid, f"{title}\n\n{(txt or '')[:1000]}", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("✅ Принять", callback_data=f"accept:{sid}"), types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{sid}"), types.InlineKeyboardButton("✉️ Ответить автору", callback_data=f"reply:{sid}")))
        else:
            bot.send_message(uid, f"{title}\nТип: {ctype}\nID файла: {fid}", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("✅ Принять", callback_data=f"accept:{sid}"), types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{sid}"), types.InlineKeyboardButton("✉️ Ответить автору", callback_data=f"reply:{sid}")))

# universal cancel
@bot.callback_query_handler(func=lambda cq: cq.data == "cancel")
def cq_cancel(cq):
    bot.answer_callback_query(cq.id, "Действие отменено.")
    pop_state(cq.from_user.id)
    bot.send_message(cq.from_user.id, "Действие отменено.", reply_markup=main_menu())

@bot.message_handler(func=lambda m: get_state(m.from_user.id) is not None)
def handle_unexpected_input(m):
    bot.send_message(m.chat.id, "Я сейчас жду конкретные данные. Отправьте их или /cancel.", reply_markup=types.ReplyKeyboardRemove())

@bot.message_handler(func=lambda m: (m.chat.type == 'private') and (get_state(m.from_user.id) is None) and (m.text is not None) and (not m.text.startswith('/')) , content_types=['text'])
def handle_private_default(m):
    bot.send_message(m.chat.id, "Чтобы войти в меню напишите /start")

# ========== WEBHOOK: Flask ==========
app = Flask(__name__)
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
        bot.remove_webhook()
    except Exception:
        pass
    try:
        ok = bot.set_webhook(url=webhook_url)
        if not ok:
            logger.error("set_webhook returned False")
    except Exception as e:
        logger.exception("Не удалось установить webhook: %s", e)
        raise

# Try to set webhook (if running under gunicorn/render)
try:
    setup_webhook()
except Exception as e:
    logger.error("Ошибка при установке webhook: %s", e)

# ========== RUN (local) ==========
if __name__ == "__main__":
    logger.info("Запуск Flask на 0.0.0.0:%s", PORT)
    app.run(host="0.0.0.0", port=PORT)
