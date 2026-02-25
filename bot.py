import os
import re
import asyncio
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
if GEMINI_API_KEY:
    from google import genai
    from google.genai import types
    gclient = genai.Client(api_key=GEMINI_API_KEY)
else:
    gclient = None

# =======================
# CONFIG
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

TZ_NAME = os.getenv("TZ", "Asia/Almaty")
TZ = ZoneInfo(TZ_NAME)

DB_PATH = os.getenv("DB_PATH", "foodbot.db")
ANTI_SPAM_SECONDS = int(os.getenv("ANTI_SPAM_SECONDS", "90"))

# Reminder times (Almaty)
WATER_HOUR = int(os.getenv("WATER_HOUR", "7"))
WATER_MIN = int(os.getenv("WATER_MIN", "0"))
STEPS_HOUR = int(os.getenv("STEPS_HOUR", "22"))
STEPS_MIN = int(os.getenv("STEPS_MIN", "0"))
WEIGH_DOW = os.getenv("WEIGH_DOW", "sun")  # sun, mon, ...
WEIGH_HOUR = int(os.getenv("WEIGH_HOUR", "10"))
WEIGH_MIN = int(os.getenv("WEIGH_MIN", "0"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# =======================
# REGEX (weight/steps)
# =======================
WEIGHT_RE = re.compile(r"(?:вес\s*)?(\d{2,3}(?:[.,]\d)?)", re.IGNORECASE)
STEPS_RE = re.compile(r"(\d{3,6})\s*(?:шаг(?:ов|а)?|steps)?", re.IGNORECASE)

DEFAULT_RULES = (
    "Я оцениваю еду по: белок / овощи(клетчатка) / сладкое / жирное / порция / соусы.\n"
    "Отвечаю форматом: Блюдо, Оценка 1–10, Калории (примерно диапазоном), Почему, Совет.\n"
    "Калории по фото — всегда приблизительно."
)

# =======================
# DB helpers
# =======================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS chats(
            chat_id INTEGER PRIMARY KEY,
            bound INTEGER DEFAULT 0,
            goal TEXT DEFAULT 'maintain'
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS weights(
            chat_id INTEGER,
            dt TEXT,
            weight REAL
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS steps(
            chat_id INTEGER,
            dt TEXT,
            steps INTEGER
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS last_actions(
            chat_id INTEGER PRIMARY KEY,
            last_food_ts INTEGER DEFAULT 0
        )""")

        await db.commit()


async def ensure_chat(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES(?)", (chat_id,))
        await db.execute("INSERT OR IGNORE INTO last_actions(chat_id) VALUES(?)", (chat_id,))
        await db.commit()


async def set_bound(chat_id: int, bound: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE chats SET bound=? WHERE chat_id=?", (bound, chat_id))
        await db.commit()


async def set_goal(chat_id: int, goal: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE chats SET goal=? WHERE chat_id=?", (goal, chat_id))
        await db.commit()


async def get_goal(chat_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT goal FROM chats WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return row[0] if row else "maintain"


async def bound_chats():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT chat_id FROM chats WHERE bound=1")
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def can_analyze_food(chat_id: int) -> bool:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT last_food_ts FROM last_actions WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        last_ts = row[0] if row else 0

        if now - last_ts < ANTI_SPAM_SECONDS:
            return False

        await db.execute("UPDATE last_actions SET last_food_ts=? WHERE chat_id=?", (now, chat_id))
        await db.commit()
        return True


async def save_weight(chat_id: int, w: float):
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO weights(chat_id, dt, weight) VALUES(?,?,?)", (chat_id, ts, w))
        await db.commit()


async def save_steps(chat_id: int, s: int):
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO steps(chat_id, dt, steps) VALUES(?,?,?)", (chat_id, ts, s))
        await db.commit()


async def last_weight(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT dt, weight FROM weights WHERE chat_id=? ORDER BY dt DESC LIMIT 1",
            (chat_id,),
        )
        return await cur.fetchone()  # (dt, weight) or None


async def weight_at_or_before(chat_id: int, dt_limit: datetime):
    lim = dt_limit.astimezone(TZ).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT dt, weight FROM weights
            WHERE chat_id=? AND dt <= ?
            ORDER BY dt DESC LIMIT 1
        """, (chat_id, lim))
        return await cur.fetchone()


def weight_comment(curr: float, prev: float | None):
    if prev is None:
        return "Записал ✅ Если будешь отправлять вес регулярно (например, по воскресеньям), покажу динамику."
    diff = curr - prev
    if abs(diff) < 0.2:
        return f"Почти без изменений ({diff:+.1f} кг). Стабильно — это ок."
    if diff < 0:
        return f"Тренд вниз: {diff:+.1f} кг относительно прошлой точки. Хорошо 💪"
    return f"Тренд вверх: {diff:+.1f} кг. Часто это вода/соль/сон — смотрим по 2–3 неделям."


# =======================
# Gemini food analysis
# =======================
async def analyze_food(photo_file_id: str, goal: str) -> str:
    if not gclient:
        return (
            "Gemini анализ отключен.\n"
            "Добавь переменную GEMINI_API_KEY в Railway Variables.\n"
            "Пока можешь описать еду текстом — я дам оценку по описанию."
        )

    tg_file = await bot.get_file(photo_file_id)
    bio = await bot.download_file(tg_file.file_path)
    img_bytes = bio.read()

    strictness = {
        "cut": "Будь строже: контролируй калории/сладкое/жирное, упор на белок и овощи.",
        "maintain": "Баланс: без жесткача, но по делу.",
        "bulk": "Упор на белок и достаточную калорийность; отмечай качество продуктов."
    }.get(goal, "Баланс: без жесткача, но по делу.")

    prompt = f"""
Ты — помощник по питанию. Цель: {goal}. {strictness}

По фото еды:
1) Определи блюдо (если не уверен — 2–3 варианта).
2) Оценка 1–10.
3) Калории ДИАПАЗОНОМ (примерно).
4) Почему (1–2 предложения).
5) Один конкретный совет.

Формат строго:
Блюдо:
Оценка:
Калории:
Почему:
Совет:
"""

    # Telegram обычно отдаёт JPEG. Если вдруг PNG — Gemini всё равно обычно примет, но mime укажем jpeg.
    resp = gclient.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            prompt,
            types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
        ],
    )

    text = (resp.text or "").strip()
    return text if text else "Не смог распознать блюдо по фото 😅 Попробуй другое фото или подпиши, что это."


# =======================
# Commands
# =======================
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    await msg.reply(
        "Я на месте ✅\n"
        "Кидай фото еды — оценю и прикину калории.\n"
        "Команды: /bind /unbind /goal /rules /stats"
    )


@dp.message(Command("bind"))
async def cmd_bind(msg: Message):
    if msg.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await msg.reply("Эта команда нужна в группе.")

    await ensure_chat(msg.chat.id)
    await set_bound(msg.chat.id, 1)
    await msg.reply("Ок! Напоминания включены для этой группы ✅")


@dp.message(Command("unbind"))
async def cmd_unbind(msg: Message):
    if msg.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await msg.reply("Эта команда нужна в группе.")

    await ensure_chat(msg.chat.id)
    await set_bound(msg.chat.id, 0)
    await msg.reply("Ок! Напоминания выключены для этой группы ✅")


@dp.message(Command("goal"))
async def cmd_goal(msg: Message):
    await ensure_chat(msg.chat.id)
    parts = (msg.text or "").split()

    if len(parts) < 2 or parts[1] not in {"cut", "maintain", "bulk"}:
        return await msg.reply("Формат: /goal cut | maintain | bulk")

    await set_goal(msg.chat.id, parts[1])
    await msg.reply(f"Цель установлена: {parts[1]} ✅")


@dp.message(Command("rules"))
async def cmd_rules(msg: Message):
    await msg.reply(DEFAULT_RULES)


@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    await ensure_chat(msg.chat.id)
    lw = await last_weight(msg.chat.id)
    if not lw:
        return await msg.reply("Пока нет записей веса. Напиши, например: 79.4")

    dt_now = datetime.now(TZ)
    w_now = float(lw[1])

    w_7 = await weight_at_or_before(msg.chat.id, dt_now - timedelta(days=7))
    w_30 = await weight_at_or_before(msg.chat.id, dt_now - timedelta(days=30))

    lines = [f"Последний вес: {w_now:.1f} кг ({lw[0]})"]
    if w_7:
        lines.append(f"Изменение за 7 дней: {w_now - float(w_7[1]):+.1f} кг")
    if w_30:
        lines.append(f"Изменение за 30 дней: {w_now - float(w_30[1]):+.1f} кг")

    await msg.reply("\n".join(lines))


# =======================
# Group handlers
# =======================
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & F.photo)
async def on_food_photo(msg: Message):
    await ensure_chat(msg.chat.id)
    if not await can_analyze_food(msg.chat.id):
        return  # антиспам: молча

    goal = await get_goal(msg.chat.id)
    try:
        text = await analyze_food(msg.photo[-1].file_id, goal)
        await msg.reply(text)
    except Exception:
        await msg.reply("Не смог обработать фото 😅 Попробуй другое или подпиши, что на тарелке.")


@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & F.text)
async def on_text(msg: Message):
    await ensure_chat(msg.chat.id)
    t = (msg.text or "").strip()

    # вес
    mw = WEIGHT_RE.fullmatch(t) or WEIGHT_RE.search(t)
    if mw:
        raw = mw.group(1).replace(",", ".")
        try:
            w = float(raw)
        except ValueError:
            w = None

        if w and 30.0 <= w <= 300.0:
            await save_weight(msg.chat.id, w)
            prev_row = await weight_at_or_before(msg.chat.id, datetime.now(TZ) - timedelta(days=6))
            prev = float(prev_row[1]) if prev_row else None
            return await msg.reply(f"Вес: {w:.1f} кг ✅\n{weight_comment(w, prev)}")

    # шаги
    ms = STEPS_RE.fullmatch(t) or STEPS_RE.search(t)
    if ms:
        s = int(ms.group(1))
        if 300 <= s <= 100000:
            await save_steps(msg.chat.id, s)
            return await msg.reply(f"Шаги: {s} ✅")


# =======================
# Reminders
# =======================
async def send_to_bound(text: str):
    for chat_id in await bound_chats():
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            pass


def setup_scheduler():
    sched = AsyncIOScheduler(timezone=TZ)

    sched.add_job(
        send_to_bound,
        "cron",
        hour=WATER_HOUR,
        minute=WATER_MIN,
        args=["🥤 07:00 — стакан воды."],
    )

    sched.add_job(
        send_to_bound,
        "cron",
        hour=STEPS_HOUR,
        minute=STEPS_MIN,
        args=["🚶 22:00 — скинь скрин шагов (или напиши цифрой)."],
    )

    sched.add_job(
        send_to_bound,
        "cron",
        day_of_week=WEIGH_DOW,
        hour=WEIGH_HOUR,
        minute=WEIGH_MIN,
        args=["⚖️ Воскресенье — взвешивание. Скинь фото весов или напиши вес цифрой (например: 79.4)."],
    )

    sched.start()


async def main():
    await init_db()
    setup_scheduler()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())