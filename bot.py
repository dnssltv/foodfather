import os
import re
import base64
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from openai import OpenAI

# =======================
# CONFIG
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

TZ_NAME = os.getenv("TZ", "Asia/Almaty")
TZ = ZoneInfo(TZ_NAME)

DB_PATH = os.getenv("DB_PATH", "foodbot.db")  # Railway Volume: /data/foodbot.db
DEBUG = os.getenv("DEBUG", "0").strip() == "1"

# Groq (OpenAI-compatible)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct").strip()
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

groq_client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL) if GROQ_API_KEY else None

# Reminders (Almaty)
WATER_HOUR = int(os.getenv("WATER_HOUR", "7"))
WATER_MIN = int(os.getenv("WATER_MIN", "0"))
STEPS_HOUR = int(os.getenv("STEPS_HOUR", "22"))
STEPS_MIN = int(os.getenv("STEPS_MIN", "0"))
WEIGH_DOW = os.getenv("WEIGH_DOW", "sun")
WEIGH_HOUR = int(os.getenv("WEIGH_HOUR", "10"))
WEIGH_MIN = int(os.getenv("WEIGH_MIN", "0"))

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=storage)

# =======================
# REGEX / INTENTS
# =======================
WEIGHT_RE = re.compile(r"(?:^|\b)(?:вес\s*)?(\d{2,3}(?:[.,]\d)?)\b", re.IGNORECASE)
STEPS_RE = re.compile(r"(?:^|\b)(\d{3,6})\s*(?:шаг(?:ов|а)?|steps)?\b", re.IGNORECASE)

ASK_MY_WEIGHT_RE = re.compile(r"(какой\s+мой\s+вес|мой\s+вес\s+сейчас|сколько\s+я\s+вешу)", re.IGNORECASE)
ASK_EATEN_TODAY_RE = re.compile(r"(сколько\s+я\s+съел|сколько\s+я\s+съела|сколько\s+калори(й|и)\s+сегодня\s+съел|сколько\s+калори(й|и)\s+сегодня\s+съела|сколько\s+я\s+съел\s+сегодня|сколько\s+я\s+съела\s+сегодня)", re.IGNORECASE)
ASK_BURNED_TODAY_RE = re.compile(r"(сколько\s+я\s+сж(е|ё)г|сколько\s+я\s+израсходовал|сколько\s+я\s+потратил|калори(й|и)\s+сж(е|ё)г\s+сегодня|израсходовал\s+сегодня|потратил\s+сегодня)", re.IGNORECASE)
ASK_BALANCE_RE = re.compile(r"(баланс\s+калори(й|и)|профицит|дефицит)\b", re.IGNORECASE)

# Parse calories line: "Калории: 650-850 ккал"
CAL_RANGE_RE = re.compile(r"Калор(ии|ий|ии):\s*([0-9]{2,4})\s*[-–]\s*([0-9]{2,4})", re.IGNORECASE)

DEFAULT_RULES = (
    "Я оцениваю еду по: белок / овощи(клетчатка) / сладкое / жирное / порция / соусы.\n"
    "Отвечаю форматом:\n"
    "Блюдо / Оценка 1–10 / Калории (примерно диапазоном) / Почему / Совет.\n"
    "Калории по фото — всегда приблизительно."
)

# =======================
# FSM: profile
# =======================
class ProfileFlow(StatesGroup):
    name = State()
    height = State()
    weight = State()

# =======================
# Helpers
# =======================
def mention_user_html(msg: Message, fallback_name: str) -> str:
    u = msg.from_user
    if u and u.username:
        return f"@{u.username}"
    safe_name = (fallback_name or "пользователь").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={u.id}">{safe_name}</a>'

def guess_mime(file_path: str) -> str:
    fp = (file_path or "").lower()
    if fp.endswith(".png"):
        return "image/png"
    if fp.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"

def to_data_url(img_bytes: bytes, mime: str) -> str:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64}"

def parse_kcal_range(text: str):
    m = CAL_RANGE_RE.search(text or "")
    if not m:
        return (None, None)
    low = int(m.group(2))
    high = int(m.group(3))
    if low > high:
        low, high = high, low
    return (low, high)

def kcal_mid(low, high):
    if low is None or high is None:
        return None
    return int(round((low + high) / 2))

def estimate_burned_kcal_from_steps(steps: int, weight_kg: float | None):
    # очень грубо: 10k шагов ~ 400 ккал для 70 кг
    base_per_step = 0.04
    factor = (weight_kg / 70.0) if weight_kg else 1.0
    return int(round(steps * base_per_step * factor))

def snacking_warning(meals_rows):
    # meals_rows: list of (dt, title, low, high) sorted ASC
    if not meals_rows:
        return None

    if len(meals_rows) >= 5:
        return ("Похоже, сегодня очень часто ешь (много перекусов). "
                "Если это «на автомате», попробуй: 2–3 основных приёма + один нормальный перекус "
                "(белок + клетчатка), чтобы реже тянуло есть.")

    times = []
    for dt_str, *_ in meals_rows:
        try:
            times.append(datetime.fromisoformat(dt_str).astimezone(TZ))
        except Exception:
            pass

    for i in range(len(times) - 2):
        if (times[i + 2] - times[i]) <= timedelta(hours=2):
            return ("Вижу несколько приёмов пищи очень близко по времени. "
                    "Если хочешь — сделаем перекус более «сытным» (белок + клетчатка), "
                    "чтобы реже хотелось есть.")
    return None

# =======================
# DB
# =======================
async def init_db():
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

        await db.execute("""
        CREATE TABLE IF NOT EXISTS meals(
            chat_id INTEGER,
            user_id INTEGER,
            dt TEXT,
            title TEXT,
            kcal_low INTEGER,
            kcal_high INTEGER
        )""")

        await db.commit()

async def ensure_chat(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES(?)", (chat_id,))
        await db.commit()

async def set_bound(chat_id: int, bound: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE chats SET bound=? WHERE chat_id=?", (bound, chat_id))
        await db.commit()

async def bound_chats():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT chat_id FROM chats WHERE bound=1")
        rows = await cur.fetchall()
        return [r[0] for r in rows]

async def set_goal(chat_id: int, goal: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE chats SET goal=? WHERE chat_id=?", (goal, chat_id))
        await db.commit()

async def get_goal(chat_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT goal FROM chats WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return row[0] if row else "maintain"

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
        await db.execute("INSERT INTO weights(chat_id, user_id, dt, weight) VALUES(?,?,?,?)",
                         (chat_id, user_id, ts, w))
        await db.commit()

async def last_weight(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT dt, weight FROM weights WHERE chat_id=? AND user_id=? ORDER BY dt DESC LIMIT 1",
            (chat_id, user_id),
        )
        return await cur.fetchone()

async def save_steps(chat_id: int, user_id: int, s: int):
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO steps(chat_id, user_id, dt, steps) VALUES(?,?,?,?)",
                         (chat_id, user_id, ts, s))
        await db.commit()

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

async def save_meal(chat_id: int, user_id: int, title: str, kcal_low: int | None, kcal_high: int | None):
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO meals(chat_id, user_id, dt, title, kcal_low, kcal_high) VALUES(?,?,?,?,?,?)",
            (chat_id, user_id, ts, title, kcal_low, kcal_high)
        )
        await db.commit()

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

# =======================
# Groq Vision analyze
# =======================
async def analyze_food(photo_file_id: str, goal: str, user_context: str, caption: str | None):
    if not groq_client:
        return "⚠️ Groq не настроен: добавь GROQ_API_KEY в Railway Variables."

    tg_file = await bot.get_file(photo_file_id)
    bio = await bot.download_file(tg_file.file_path)
    img_bytes = bio.read()
    mime = guess_mime(tg_file.file_path)
    data_url = to_data_url(img_bytes, mime)

    strictness = {
        "cut": "Будь строже: меньше масла/сладкого/соусов, упор на белок и овощи.",
        "maintain": "Баланс: по делу, без жесткача.",
        "bulk": "Упор на белок и качество еды, без мусора."
    }.get(goal, "Баланс: по делу, без жесткача.")

    cap = (caption or "").strip()
    caption_line = f"Подпись к фото: {cap}" if cap else "Подписи нет."

    prompt = f"""
Ты — помощник по питанию. {strictness}
Контекст о человеке (если есть): {user_context}
{caption_line}

По фото еды:
1) Определи блюдо (если не уверен — 2–3 варианта).
2) Оценка 1–10.
3) Калории диапазоном (примерно, форматом: Калории: 650-850 ккал).
4) Почему (1–2 предложения).
5) 1 конкретный совет.

Без давления и жестких диет.
Формат строго:
Блюдо:
Оценка:
Калории:
Почему:
Совет:
""".strip()

    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            temperature=0.3,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text if text else "Не смог распознать по фото 😅 Попробуй другое фото или подпиши."
    except Exception as e:
        err = repr(e)
        print("Groq error:", err)

        low = err.lower()
        hint = "Не смог обработать фото 😅"
        if "401" in low or "unauthorized" in low or "invalid api key" in low:
            hint = "Не могу обратиться к Groq: проблема с API ключом (проверь GROQ_API_KEY)."
        elif "429" in low or "rate" in low or "quota" in low:
            hint = "Groq ограничил запросы (лимит/частота). Подожди немного и попробуй снова."
        elif "model" in low and ("not found" in low or "does not exist" in low):
            hint = "Модель Groq не найдена. Проверь GROQ_MODEL."
        elif "timeout" in low:
            hint = "Groq долго отвечает (таймаут). Попробуй ещё раз через 10–20 секунд."

        if DEBUG:
            return f"⚠️ {hint}\n\nDEBUG: {err[:240]}"
        return f"⚠️ {hint}\nСовет: добавь подпись к фото — так точнее."

# =======================
# Commands / Profile
# =======================
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    await msg.reply(
        "Я на месте ✅\n"
        "Кидай фото еды — оценю и прикину калории.\n"
        "Профиль: /profile (в личке) → затем в группе /linkprofile\n"
        "Команды: /bind /unbind /goal /rules"
    )

@dp.message(Command("rules"))
async def cmd_rules(msg: Message):
    await msg.reply(DEFAULT_RULES)

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

@dp.message(Command("profile"))
async def cmd_profile(msg: Message, state: FSMContext):
    if msg.chat.type != ChatType.PRIVATE:
        return await msg.reply("Напиши мне в личку /profile — я задам 3 вопроса и запомню данные 🙂")
    await state.set_state(ProfileFlow.name)
    await msg.reply("Как тебя называть? (например: Denis)")

@dp.message(ProfileFlow.name)
async def prof_name(msg: Message, state: FSMContext):
    name = (msg.text or "").strip()
    if not name or len(name) > 30:
        return await msg.reply("Коротко имя (до 30 символов).")
    await state.update_data(name=name)
    await state.set_state(ProfileFlow.height)
    await msg.reply("Рост в см? (например: 188)")

@dp.message(ProfileFlow.height)
async def prof_height(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip()
    if not raw.isdigit():
        return await msg.reply("Рост цифрами, например: 188")
    h = int(raw)
    if h < 120 or h > 230:
        return await msg.reply("Похоже на ошибку. Рост в см (пример: 188).")
    await state.update_data(height=h)
    await state.set_state(ProfileFlow.weight)
    await msg.reply("Вес в кг? (например: 82.4)")

@dp.message(ProfileFlow.weight)
async def prof_weight(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip().replace(",", ".")
    try:
        w = float(raw)
    except ValueError:
        return await msg.reply("Вес числом, например: 82.4")
    if w < 30 or w > 300:
        return await msg.reply("Похоже на ошибку. Вес в кг (пример: 82.4).")

    data = await state.get_data()
    name = data.get("name")
    height = data.get("height")
    user_id = msg.from_user.id

    await upsert_profile(0, user_id, name, int(height), float(w))
    await state.clear()
    await msg.reply(f"Ок, {name}! Сохранил ✅\nТеперь в группе напиши /linkprofile")

@dp.message(Command("linkprofile"))
async def cmd_linkprofile(msg: Message):
    if msg.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await msg.reply("Эта команда нужна в группе.")
    await ensure_chat(msg.chat.id)

    user_id = msg.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name, height_cm, weight_kg FROM profiles WHERE chat_id=0 AND user_id=?",
                               (user_id,))
        row = await cur.fetchone()

    if not row:
        return await msg.reply("Сначала заполни профиль в личке: /profile")

    name, h, w = row
    await upsert_profile(msg.chat.id, user_id, name, int(h), float(w))
    await msg.reply(f"{name}, профиль привязан к этой группе ✅")

# =======================
# Q&A in group
# =======================
async def answer_questions(msg: Message, mention: str, prof):
    chat_id = msg.chat.id
    user_id = msg.from_user.id
    text = (msg.text or "").strip()

    if ASK_MY_WEIGHT_RE.search(text):
        lw = await last_weight(chat_id, user_id)
        if not lw:
            await msg.reply(f"{mention}, у меня пока нет твоего веса. Напиши, например: 82.4")
            return True
        await msg.reply(f"{mention}, последний записанный вес: {float(lw[1]):.1f} кг ({lw[0]})")
        return True

    if ASK_EATEN_TODAY_RE.search(text):
        rows = await meals_today(chat_id, user_id)
        if not rows:
            await msg.reply(f"{mention}, сегодня нет записанных приёмов пищи. Кинь фото еды 🙂")
            return True
        total = 0
        known = 0
        for _, _, low, high in rows:
            mid = kcal_mid(low, high)
            if mid is not None:
                total += mid
                known += 1
        if known == 0:
            await msg.reply(f"{mention}, приёмы есть, но без калорий. Кинь фото с подписью — будет точнее.")
            return True
        await msg.reply(f"{mention}, примерно съедено сегодня: ~{total} ккал (по {known} приёмам).")
        return True

    if ASK_BURNED_TODAY_RE.search(text):
        steps = await steps_today(chat_id, user_id)
        weight_kg = float(prof[2]) if prof else None
        burned = estimate_burned_kcal_from_steps(steps, weight_kg)
        await msg.reply(f"{mention}, сегодня шагов: {steps} → примерно потрачено {burned} ккал (очень грубо).")
        return True

    if ASK_BALANCE_RE.search(text):
        rows = await meals_today(chat_id, user_id)
        intake = 0
        known = 0
        for _, _, low, high in rows:
            mid = kcal_mid(low, high)
            if mid is not None:
                intake += mid
                known += 1

        steps = await steps_today(chat_id, user_id)
        weight_kg = float(prof[2]) if prof else None
        burned = estimate_burned_kcal_from_steps(steps, weight_kg)
        balance = intake - burned
        sign = "+" if balance > 0 else ""
        await msg.reply(f"{mention}, баланс сегодня (очень примерно): {sign}{balance} ккал.\nСъел ~{intake}, Сжёг шагами ~{burned}.")
        return True

    return False

# =======================
# Handlers: photos + text
# =======================
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & F.photo)
async def on_food_photo(msg: Message):
    await ensure_chat(msg.chat.id)

    user_id = msg.from_user.id
    prof = await get_profile(msg.chat.id, user_id)
    name = prof[0] if prof else (msg.from_user.first_name or "Ты")
    mention = mention_user_html(msg, name)

    user_context = "нет"
    if prof:
        user_context = f"Имя: {prof[0]}, Рост: {prof[1]} см, Вес: {prof[2]} кг"

    goal = await get_goal(msg.chat.id)

    analysis = await analyze_food(msg.photo[-1].file_id, goal, user_context, msg.caption)

    low, high = parse_kcal_range(analysis)

    title = (msg.caption or "").strip()
    if not title:
        m = re.search(r"Блюдо:\s*(.+)", analysis)
        title = m.group(1).strip() if m else "Еда"

    await save_meal(msg.chat.id, user_id, title, low, high)

    today_rows = await meals_today(msg.chat.id, user_id)
    warn = snacking_warning(today_rows)

    out = f"{mention}, вот что вижу:\n\n{analysis}"
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
    mention = mention_user_html(msg, name)

    if await answer_questions(msg, mention, prof):
        return

    mw = WEIGHT_RE.search(t)
    if mw:
        raw = mw.group(1).replace(",", ".")
        try:
            w = float(raw)
        except ValueError:
            w = None
        if w and 30.0 <= w <= 300.0:
            await save_weight(msg.chat.id, user_id, w)
            return await msg.reply(f"{mention}, вес записал: {w:.1f} кг ✅")

    ms = STEPS_RE.search(t)
    if ms:
        s = int(ms.group(1))
        if 300 <= s <= 100000:
            await save_steps(msg.chat.id, user_id, s)
            return await msg.reply(f"{mention}, шаги записал: {s} ✅")

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
    sched.add_job(send_to_bound, "cron", hour=WATER_HOUR, minute=WATER_MIN, args=["🥤 07:00 — стакан воды."])
    sched.add_job(send_to_bound, "cron", hour=STEPS_HOUR, minute=STEPS_MIN, args=["🚶 22:00 — скинь скрин шагов (или напиши цифрой)."])
    sched.add_job(send_to_bound, "cron", day_of_week=WEIGH_DOW, hour=WEIGH_HOUR, minute=WEIGH_MIN, args=["⚖️ Взвешивание: скинь фото весов или напиши вес (например: 79.4)."])
    sched.start()

async def main():
    await init_db()
    setup_scheduler()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
