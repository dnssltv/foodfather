import os
import re
import asyncio
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# =======================
# CONFIG
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

TZ_NAME = os.getenv("TZ", "Asia/Almaty")
TZ = ZoneInfo(TZ_NAME)

DB_PATH = os.getenv("DB_PATH", "foodbot.db")  # <-- для Railway Volume ставь /data/foodbot.db
ANTI_SPAM_SECONDS = int(os.getenv("ANTI_SPAM_SECONDS", "90"))

DEBUG = os.getenv("DEBUG", "0").strip() == "1"

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()

if GEMINI_API_KEY:
    from google import genai
    from google.genai import types
    gclient = genai.Client(api_key=GEMINI_API_KEY)
else:
    gclient = None

# Reminders (Almaty)
WATER_HOUR = int(os.getenv("WATER_HOUR", "7"))
WATER_MIN = int(os.getenv("WATER_MIN", "0"))
STEPS_HOUR = int(os.getenv("STEPS_HOUR", "22"))
STEPS_MIN = int(os.getenv("STEPS_MIN", "0"))
WEIGH_DOW = os.getenv("WEIGH_DOW", "sun")
WEIGH_HOUR = int(os.getenv("WEIGH_HOUR", "10"))
WEIGH_MIN = int(os.getenv("WEIGH_MIN", "0"))

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# =======================
# REGEX / TEXT INTENTS
# =======================
WEIGHT_RE = re.compile(r"(?:^|\b)(?:вес\s*)?(\d{2,3}(?:[.,]\d)?)\b", re.IGNORECASE)
STEPS_RE = re.compile(r"(?:^|\b)(\d{3,6})\s*(?:шаг(?:ов|а)?|steps)?\b", re.IGNORECASE)

ASK_MY_WEIGHT_RE = re.compile(r"(какой\s+мой\s+вес|мой\s+вес\s+сейчас|сколько\s+я\s+вешу)", re.IGNORECASE)
ASK_EATEN_TODAY_RE = re.compile(r"(сколько\s+я\s+съел|сколько\s+я\s+съела|калори(й|и)\s+съел|калори(й|и)\s+съела|сколько\s+калори(й|и)\s+сегодня\s+съел|сколько\s+калори(й|и)\s+сегодня\s+съела)", re.IGNORECASE)
ASK_BURNED_TODAY_RE = re.compile(r"(сколько\s+я\s+сж(е|ё)г|сколько\s+я\s+сж(е|ё)г\s+калори(й|и)|сколько\s+я\s+израсходовал|сколько\s+я\s+потратил|калори(й|и)\s+сж(е|ё)г\s+сегодня|израсходовал\s+сегодня)", re.IGNORECASE)
ASK_BALANCE_RE = re.compile(r"(баланс\s+калори(й|и)|профицит|дефицит)\b", re.IGNORECASE)

# Calories parsing from Gemini response
CAL_RANGE_RE = re.compile(r"Калор(ии|ий|ии):\s*([0-9]{2,4})\s*[-–]\s*([0-9]{2,4})", re.IGNORECASE)

DEFAULT_RULES = (
    "Я оцениваю еду по: белок / овощи(клетчатка) / сладкое / жирное / порция / соусы.\n"
    "Отвечаю форматом: Блюдо, Оценка 1–10, Калории (примерно диапазоном), Почему, Совет.\n"
    "Калории по фото — всегда приблизительно."
)

# =======================
# FSM: profile survey
# =======================
class ProfileFlow(StatesGroup):
    name = State()
    height = State()
    weight = State()

# =======================
# DB
# =======================
async def init_db():
    # Создадим папку для DB_PATH, если путь вида /data/foodbot.db
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and db_dir != ".":
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS chats(
            chat_id INTEGER PRIMARY KEY,
            bound INTEGER DEFAULT 0,
            goal TEXT DEFAULT 'maintain'
        )""")

        # profiles: chat_id=0 — профиль из лички (глобальный)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS profiles(
            chat_id INTEGER,
            user_id INTEGER,
            name TEXT,
            height_cm INTEGER,
            weight_kg REAL,
            updated_at TEXT,
            PRIMARY KEY(chat_id, user_id)
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS weights(
            chat_id INTEGER,
            user_id INTEGER,
            dt TEXT,
            weight REAL
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS steps(
            chat_id INTEGER,
            user_id INTEGER,
            dt TEXT,
            steps INTEGER
        )""")

        # meals: хранение оцененной калорийности с фото
        await db.execute("""
        CREATE TABLE IF NOT EXISTS meals(
            chat_id INTEGER,
            user_id INTEGER,
            dt TEXT,
            title TEXT,
            kcal_low INTEGER,
            kcal_high INTEGER
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


async def upsert_profile(chat_id: int, user_id: int, name: str, height_cm: int, weight_kg: float):
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO profiles(chat_id, user_id, name, height_cm, weight_kg, updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            name=excluded.name,
            height_cm=excluded.height_cm,
            weight_kg=excluded.weight_kg,
            updated_at=excluded.updated_at
        """, (chat_id, user_id, name, height_cm, weight_kg, ts))
        await db.commit()


async def get_profile(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT name, height_cm, weight_kg, updated_at
            FROM profiles WHERE chat_id=? AND user_id=?
        """, (chat_id, user_id))
        return await cur.fetchone()


async def save_weight(chat_id: int, user_id: int, w: float):
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO weights(chat_id, user_id, dt, weight) VALUES(?,?,?,?)", (chat_id, user_id, ts, w))
        await db.commit()


async def save_steps(chat_id: int, user_id: int, s: int):
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO steps(chat_id, user_id, dt, steps) VALUES(?,?,?,?)", (chat_id, user_id, ts, s))
        await db.commit()


async def save_meal(chat_id: int, user_id: int, title: str, kcal_low: int | None, kcal_high: int | None):
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO meals(chat_id, user_id, dt, title, kcal_low, kcal_high) VALUES(?,?,?,?,?,?)",
            (chat_id, user_id, ts, title, kcal_low, kcal_high)
        )
        await db.commit()


async def last_weight(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT dt, weight FROM weights WHERE chat_id=? AND user_id=? ORDER BY dt DESC LIMIT 1",
            (chat_id, user_id),
        )
        return await cur.fetchone()


async def weight_at_or_before(chat_id: int, user_id: int, dt_limit: datetime):
    lim = dt_limit.astimezone(TZ).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT dt, weight FROM weights
            WHERE chat_id=? AND user_id=? AND dt <= ?
            ORDER BY dt DESC LIMIT 1
        """, (chat_id, user_id, lim))
        return await cur.fetchone()


async def steps_today(chat_id: int, user_id: int) -> int:
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    end = datetime.now(TZ).replace(hour=23, minute=59, second=59, microsecond=0).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT COALESCE(SUM(steps), 0) FROM steps
            WHERE chat_id=? AND user_id=? AND dt BETWEEN ? AND ?
        """, (chat_id, user_id, start, end))
        row = await cur.fetchone()
        return int(row[0] or 0)


async def meals_today(chat_id: int, user_id: int):
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    end = datetime.now(TZ).replace(hour=23, minute=59, second=59, microsecond=0).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT dt, title, kcal_low, kcal_high FROM meals
            WHERE chat_id=? AND user_id=? AND dt BETWEEN ? AND ?
            ORDER BY dt ASC
        """, (chat_id, user_id, start, end))
        return await cur.fetchall()


def weight_comment(curr: float, prev: float | None):
    if prev is None:
        return "Записал ✅ Если будешь присылать вес регулярно, покажу динамику."
    diff = curr - prev
    if abs(diff) < 0.2:
        return f"Почти без изменений ({diff:+.1f} кг). Стабильно — это ок."
    if diff < 0:
        return f"Тренд вниз: {diff:+.1f} кг. Хорошо 💪"
    return f"Тренд вверх: {diff:+.1f} кг. Часто влияет вода/соль/сон — смотрим по 2–3 неделям."


def steps_comment(steps: int):
    if steps >= 10000:
        return "Отлично! Активность на очень хорошем уровне."
    if steps >= 7000:
        return "Хорошо. Если хочешь усилить — попробуй +1000 завтра."
    if steps >= 4000:
        return "Норм старт. Маленькая цель на завтра: +1000 шагов."
    return "День был спокойный. Если получится — 10–15 минут прогулки вечером уже помогают."


def guess_mime(file_path: str) -> str:
    fp = (file_path or "").lower()
    if fp.endswith(".png"):
        return "image/png"
    if fp.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def parse_kcal_range(text: str):
    """
    Возвращает (low, high) или (None, None)
    """
    if not text:
        return (None, None)
    m = CAL_RANGE_RE.search(text)
    if not m:
        return (None, None)
    low = int(m.group(2))
    high = int(m.group(3))
    if low > high:
        low, high = high, low
    return (low, high)


def estimate_burned_kcal_from_steps(steps: int, weight_kg: float | None):
    """
    Очень грубо:
    ~0.04 ккал/шаг для ~70 кг.
    Масштабируем по весу.
    10k шагов ~ 400 ккал (для ~70 кг)
    """
    base_per_step = 0.04
    factor = (weight_kg / 70.0) if weight_kg else 1.0
    return int(round(steps * base_per_step * factor))


def kcal_mid(low: int | None, high: int | None):
    if low is None or high is None:
        return None
    return int(round((low + high) / 2))


def snacking_warning(meals_rows):
    """
    meals_rows: list of (dt, title, low, high) sorted asc
    Мягкий детект частых перекусов:
    - если >=5 приемов за день
    - или 3+ приема в пределах 2 часов
    """
    if not meals_rows:
        return None

    if len(meals_rows) >= 5:
        return "Похоже, сегодня много перекусов/приёмов пищи. Если чувствуешь, что это «на автомате», попробуй: запланировать 2–3 основных приёма и держать под рукой один нормальный перекус (йогурт/фрукты/орехи)."

    # проверим плотность: 3 приема за 2 часа
    times = []
    for dt_str, *_ in meals_rows:
        try:
            times.append(datetime.fromisoformat(dt_str).astimezone(TZ))
        except Exception:
            pass

    for i in range(len(times) - 2):
        if (times[i + 2] - times[i]) <= timedelta(hours=2):
            return "Вижу несколько приёмов пищи очень близко по времени. Возможно, это частые перекусы. Если хочешь — можно сделать перекус более «сытным» (белок + клетчатка), чтобы не тянуло есть каждые 30–60 минут."

    return None


# =======================
# Gemini food analysis
# =======================
async def analyze_food(photo_file_id: str, goal: str, user_context: str, caption: str | None = None) -> str:
    if not gclient:
        return (
            "Gemini анализ отключен.\n"
            "Добавь GEMINI_API_KEY в Railway Variables.\n"
            "Пока можешь описать еду текстом — я дам фидбек."
        )

    tg_file = await bot.get_file(photo_file_id)
    bio = await bot.download_file(tg_file.file_path)
    img_bytes = bio.read()
    mime = guess_mime(tg_file.file_path)

    strictness = {
        "cut": "Будь строже: меньше масла/сладкого/соусов, упор на белок и овощи.",
        "maintain": "Баланс: по делу, без жесткача.",
        "bulk": "Упор на белок и качество еды, без мусора."
    }.get(goal, "Баланс: по делу, без жесткача.")

    caption = (caption or "").strip()
    caption_line = f"Подпись к фото от пользователя: {caption}" if caption else "Подписи нет."

    prompt = f"""
Ты — помощник по питанию. {strictness}
Контекст о человеке (если есть): {user_context}
{caption_line}

По фото еды:
1) Определи блюдо (если не уверен — 2–3 варианта).
2) Оценка 1–10.
3) Калории диапазоном (примерно).
4) Почему (1–2 предложения).
5) 1 конкретный совет (что улучшить).

Не давай жестких диет/ограничений, без давления.
Формат строго:
Блюдо:
Оценка:
Калории:
Почему:
Совет:
"""

    try:
        resp = gclient.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                prompt,
                types.Part.from_bytes(data=img_bytes, mime_type=mime),
            ],
        )
        text = (resp.text or "").strip()
        return text if text else "Не смог распознать по фото 😅 Попробуй другое фото или подпиши, что на тарелке."
    except Exception as e:
        print("Gemini error:", repr(e))
        if DEBUG:
            return f"Не смог обработать фото (Gemini error). Подробности в Logs.\nОшибка: {repr(e)[:180]}"
        return "Не смог обработать фото 😅 Попробуй другое или подпиши, что на тарелке."


# =======================
# Commands
# =======================
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    await msg.reply(
        "Я на месте ✅\n"
        "Кидай фото еды — оценю и прикину калории.\n"
        "Профиль: /profile (в личке) → потом в группе /linkprofile\n"
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
    await msg.reply("Ок! Напоминания выключены ✅")


@dp.message(Command("goal"))
async def cmd_goal(msg: Message):
    if msg.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await msg.reply("Эту команду лучше использовать в группе.")
    await ensure_chat(msg.chat.id)
    parts = (msg.text or "").split()
    if len(parts) < 2 or parts[1] not in {"cut", "maintain", "bulk"}:
        return await msg.reply("Формат: /goal cut | maintain | bulk")
    await set_goal(msg.chat.id, parts[1])
    await msg.reply(f"Цель группы установлена: {parts[1]} ✅")


@dp.message(Command("rules"))
async def cmd_rules(msg: Message):
    await msg.reply(DEFAULT_RULES)


@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if msg.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await msg.reply("Эта команда работает в группе.")
    await ensure_chat(msg.chat.id)

    user_id = msg.from_user.id
    prof = await get_profile(msg.chat.id, user_id)
    name = prof[0] if prof else (msg.from_user.first_name or "Ты")

    lw = await last_weight(msg.chat.id, user_id)
    if not lw:
        return await msg.reply(f"{name}, пока нет записей веса. Напиши, например: 79.4")

    dt_now = datetime.now(TZ)
    w_now = float(lw[1])
    w_7 = await weight_at_or_before(msg.chat.id, user_id, dt_now - timedelta(days=7))
    w_30 = await weight_at_or_before(msg.chat.id, user_id, dt_now - timedelta(days=30))

    lines = [f"{name}, последний вес: {w_now:.1f} кг ({lw[0]})"]
    if w_7:
        lines.append(f"Изменение за 7 дней: {w_now - float(w_7[1]):+.1f} кг")
    if w_30:
        lines.append(f"Изменение за 30 дней: {w_now - float(w_30[1]):+.1f} кг")
    await msg.reply("\n".join(lines))


# =======================
# Profile flow (PRIVATE)
# =======================
@dp.message(Command("profile"))
async def cmd_profile(msg: Message, state: FSMContext):
    if msg.chat.type != ChatType.PRIVATE:
        return await msg.reply("Напиши мне в личку команду /profile — я задам 3 вопроса и запомню данные 🙂")
    await state.set_state(ProfileFlow.name)
    await msg.reply("Как тебя называть? (например: Денис)")


@dp.message(ProfileFlow.name)
async def prof_name(msg: Message, state: FSMContext):
    name = (msg.text or "").strip()
    if not name or len(name) > 30:
        return await msg.reply("Напиши коротко имя (до 30 символов).")
    await state.update_data(name=name)
    await state.set_state(ProfileFlow.height)
    await msg.reply("Рост в сантиметрах? (например: 188)")


@dp.message(ProfileFlow.height)
async def prof_height(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip()
    if not raw.isdigit():
        return await msg.reply("Введи рост цифрами, например: 188")
    h = int(raw)
    if h < 120 or h > 230:
        return await msg.reply("Похоже на ошибку. Введи рост в см (пример: 188).")
    await state.update_data(height=h)
    await state.set_state(ProfileFlow.weight)
    await msg.reply("Вес в кг? (например: 82.4)")


@dp.message(ProfileFlow.weight)
async def prof_weight(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip().replace(",", ".")
    try:
        w = float(raw)
    except ValueError:
        return await msg.reply("Введи вес числом, например: 82.4")
    if w < 30 or w > 300:
        return await msg.reply("Похоже на ошибку. Введи вес в кг (пример: 82.4).")

    data = await state.get_data()
    name = data.get("name")
    height = data.get("height")
    if not name or not height:
        await state.clear()
        return await msg.reply("Что-то пошло не так. Напиши /profile ещё раз.")

    user_id = msg.from_user.id
    await upsert_profile(0, user_id, name, int(height), float(w))
    await state.clear()

    await msg.reply(
        f"Ок, {name}! Сохранил: рост {height} см, вес {w:.1f} кг ✅\n\n"
        "Теперь в группе напиши /linkprofile — и я начну обращаться по имени."
    )


@dp.message(Command("linkprofile"))
async def cmd_linkprofile(msg: Message):
    if msg.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await msg.reply("Эта команда нужна в группе.")
    await ensure_chat(msg.chat.id)

    user_id = msg.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT name, height_cm, weight_kg FROM profiles WHERE chat_id=0 AND user_id=?",
            (user_id,),
        )
        row = await cur.fetchone()

    if not row:
        return await msg.reply("Сначала заполни профиль в личке: открой бота и напиши /profile")

    name, h, w = row
    await upsert_profile(msg.chat.id, user_id, name, int(h), float(w))
    await msg.reply(f"{name}, профиль привязан к этой группе ✅")


# =======================
# Q&A (text questions)
# =======================
async def answer_questions(msg: Message, name: str, profile_row):
    chat_id = msg.chat.id
    user_id = msg.from_user.id
    text = (msg.text or "").strip()

    # 1) какой мой вес
    if ASK_MY_WEIGHT_RE.search(text):
        lw = await last_weight(chat_id, user_id)
        if not lw:
            return await msg.reply(f"{name}, у меня пока нет твоего веса. Напиши, например: 82.4")
        return await msg.reply(f"{name}, последний записанный вес: {float(lw[1]):.1f} кг ({lw[0]})")

    # 2) сколько съел сегодня
    if ASK_EATEN_TODAY_RE.search(text):
        rows = await meals_today(chat_id, user_id)
        if not rows:
            return await msg.reply(f"{name}, сегодня у меня нет записанных приёмов пищи. Кинь фото еды — я посчитаю примерно 🙂")

        total = 0
        known = 0
        for _, _, low, high in rows:
            mid = kcal_mid(low, high)
            if mid is not None:
                total += mid
                known += 1

        if known == 0:
            return await msg.reply(f"{name}, я сохранил приёмы пищи, но без калорий (не было диапазона). Попробуй фото с подписью — будет точнее.")
        return await msg.reply(f"{name}, примерно съедено сегодня: ~{total} ккал (по {known} приёмам пищи).")

    # 3) сколько сжёг/израсходовал сегодня
    if ASK_BURNED_TODAY_RE.search(text):
        steps = await steps_today(chat_id, user_id)
        weight_kg = float(profile_row[2]) if profile_row else None
        burned = estimate_burned_kcal_from_steps(steps, weight_kg)
        return await msg.reply(f"{name}, по шагам сегодня: {steps} шагов → примерно {burned} ккал потрачено (оценка грубая).")

    # 4) баланс сегодня
    if ASK_BALANCE_RE.search(text):
        # intake
        rows = await meals_today(chat_id, user_id)
        intake = 0
        known = 0
        for _, _, low, high in rows:
            mid = kcal_mid(low, high)
            if mid is not None:
                intake += mid
                known += 1

        # burned
        steps = await steps_today(chat_id, user_id)
        weight_kg = float(profile_row[2]) if profile_row else None
        burned = estimate_burned_kcal_from_steps(steps, weight_kg)

        if known == 0 and steps == 0:
            return await msg.reply(f"{name}, пока нет данных за сегодня (ни еды, ни шагов).")

        balance = intake - burned
        sign = "+" if balance > 0 else ""
        return await msg.reply(
            f"{name}, баланс сегодня (очень примерно): {sign}{balance} ккал.\n"
            f"Съел: ~{intake} ккал, Сжёг шагами: ~{burned} ккал."
        )

    return False


# =======================
# Group handlers
# =======================
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & F.photo)
async def on_food_photo(msg: Message):
    await ensure_chat(msg.chat.id)
    if not await can_analyze_food(msg.chat.id):
        return

    user_id = msg.from_user.id
    prof = await get_profile(msg.chat.id, user_id)
    name = prof[0] if prof else (msg.from_user.first_name or "Ты")

    user_context = "нет"
    if prof:
        user_context = f"Имя: {prof[0]}, Рост: {prof[1]} см, Вес: {prof[2]} кг"

    goal = await get_goal(msg.chat.id)

    analysis = await analyze_food(
        msg.photo[-1].file_id,
        goal,
        user_context,
        caption=msg.caption,
    )

    # Попробуем вытащить калории и блюдо для БД
    low, high = parse_kcal_range(analysis)

    # Название блюда: берём подпись или первую строку "Блюдо: ..."
    title = (msg.caption or "").strip()
    if not title:
        # попробуем найти "Блюдо:"
        m = re.search(r"Блюдо:\s*(.+)", analysis)
        title = m.group(1).strip() if m else "Еда"

    await save_meal(msg.chat.id, user_id, title, low, high)

    # Частые перекусы: проверим после сохранения
    today_rows = await meals_today(msg.chat.id, user_id)
    warn = snacking_warning(today_rows)

    out = f"{name}, вот что вижу:\n\n{analysis}"
    if warn:
        out += f"\n\n🟡 {warn}"

    await msg.reply(out)


@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & F.text)
async def on_text(msg: Message):
    await ensure_chat(msg.chat.id)
    t = (msg.text or "").strip()

    user_id = msg.from_user.id
    prof = await get_profile(msg.chat.id, user_id)
    name = prof[0] if prof else (msg.from_user.first_name or "Ты")

    # Q&A
    answered = await answer_questions(msg, name, prof)
    if answered:
        return

    # weight
    mw = WEIGHT_RE.search(t)
    if mw:
        raw = mw.group(1).replace(",", ".")
        try:
            w = float(raw)
        except ValueError:
            w = None
        if w and 30.0 <= w <= 300.0:
            await save_weight(msg.chat.id, user_id, w)
            prev_row = await weight_at_or_before(msg.chat.id, user_id, datetime.now(TZ) - timedelta(days=6))
            prev = float(prev_row[1]) if prev_row else None
            return await msg.reply(f"{name}, вес: {w:.1f} кг ✅\n{weight_comment(w, prev)}")

    # steps
    ms = STEPS_RE.search(t)
    if ms:
        s = int(ms.group(1))
        if 300 <= s <= 100000:
            await save_steps(msg.chat.id, user_id, s)
            return await msg.reply(f"{name}, шаги: {s} ✅\n{steps_comment(s)}")


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
        args=["⚖️ Взвешивание: скинь фото весов или напиши вес цифрой (например: 79.4)."],
    )

    sched.start()


async def main():
    await init_db()
    setup_scheduler()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
