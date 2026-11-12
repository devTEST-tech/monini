"""
Telegram-бот записи на маникюр, педикюр и наращивание (aiogram 3.x).

Что умеет:
- запись: услуга -> дата -> время -> имя -> телефон;
- у каждой услуги своя длительность, между клиентами есть перерыв;
- напоминание клиенту о визите (по умолчанию за сутки);
- отмена записи клиентом (не позднее чем за N часов до визита);
- уведомления мастеру о новых записях и отменах;
- мастер сам открывает свободные окна в Telegram (кнопка «➕ Добавить окно»);
- команды мастера: /today (записи на сегодня) и /bookings (все предстоящие).

КАК ПЕРЕДЕЛАТЬ ПОД МАСТЕРА: меняйте только блок «НАСТРОЙКИ МАСТЕРА» ниже.
Остальной код трогать не нужно.
"""

import asyncio
import html
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

load_dotenv()

# ============================================================
#  НАСТРОЙКИ ИЗ ФАЙЛА .env (токен, ID мастера, прокси)
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.isdigit()}
DB_PATH = os.getenv("DB_PATH", "manicure.db")
PROXY_URL = os.getenv("PROXY_URL", "").strip()
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# ============================================================
#  НАСТРОЙКИ МАСТЕРА  <-- МЕНЯЙТЕ ЗДЕСЬ (все тексты и цены примерные)
# ============================================================
BUSINESS_NAME = "Nail Studio «Мята»"
MASTER_NAME = "Анна"
MASTER_PHONE = "+7 900 000-00-00"
ADDRESS = "г. Москва, ул. Примерная, д. 1, кв. 10 (вход со двора, 2 этаж)"

# Ссылка на работы мастера (VK, Telegram-канал и т.п.). Пустая строка = кнопки не будет.
PORTFOLIO_URL = ""

# Услуги. id: любое короткое слово латиницей без пробелов.
# minutes: сколько времени занимает услуга (это время бот блокирует в расписании).
SERVICES = [
    {"id": "mani", "name": "Маникюр классический", "price": 1200, "minutes": 60},
    {"id": "mani_gel", "name": "Маникюр + покрытие гель-лак", "price": 2200, "minutes": 120},
    {"id": "mani_gel_remove", "name": "Снятие + маникюр + гель-лак", "price": 2500, "minutes": 150},
    {"id": "extension", "name": "Наращивание ногтей", "price": 3500, "minutes": 180},
    {"id": "pedi", "name": "Педикюр", "price": 2000, "minutes": 90},
    {"id": "pedi_gel", "name": "Педикюр + покрытие", "price": 2800, "minutes": 120},
]

WORK_START = "10:00"            # начало рабочего дня
WORK_END = "20:00"              # конец рабочего дня (услуга должна закончиться до этого времени)
WORK_DAYS = {0, 1, 2, 3, 4, 5}  # 0=Пн, 1=Вт, ... 6=Вс (здесь воскресенье выходной)
DAYS_OFF = []                   # особые выходные, например ["2026-12-31", "2027-01-01"]
DAYS_AHEAD = 14                 # на сколько дней вперёд можно записаться

# Как работают свободные окна:
# False: клиенты видят обычный график (WORK_START-WORK_END в рабочие дни) и дополнительно
#        окна, которые мастер добавил сам (например, в выходной или вечером).
# True:  клиенты видят ТОЛЬКО окна, которые мастер открыл в боте командой «➕ Добавить окно».
OPEN_ONLY_BY_ADMIN = False

SLOT_STEP_MINUTES = 30          # время начала записи кратно 30 минутам (10:00, 10:30, ...)
BUFFER_MINUTES = 15             # перерыв между клиентами (уборка, подготовка)
MIN_NOTICE_MINUTES = 60         # нельзя записаться ближе чем за час до начала
CANCEL_BEFORE_HOURS = 3         # онлайн-отмена возможна не позднее чем за N часов
REMINDER_HOURS = 24             # напомнить клиенту за N часов до визита

CONTACTS = f"📍 {ADDRESS}\n📞 {MASTER_PHONE}\n🕒 Пн–Сб, {WORK_START}–{WORK_END}"

# Частые вопросы: (вопрос, ответ). Замените на реальные правила мастера.
FAQ = [
    (
        "Нужно ли снимать старое покрытие заранее?",
        "Не нужно. Если покрытие делал другой мастер или у меня давно, выберите услугу "
        "«Снятие + маникюр + гель-лак». Снятие занимает дополнительное время.",
    ),
    (
        "Что, если я опаздываю?",
        f"Пожалуйста, предупредите по телефону {MASTER_PHONE}. При опоздании более 15 минут "
        "услугу придётся сократить или перенести.",
    ),
    (
        "Как отменить или перенести запись?",
        f"Откройте «📋 Мои записи» и нажмите «Отменить», затем запишитесь на новое время. "
        f"Онлайн отменить можно не позднее чем за {CANCEL_BEFORE_HOURS} ч до визита, "
        f"позже позвоните: {MASTER_PHONE}.",
    ),
    ("Как оплатить?", "Наличными, переводом или картой после услуги."),
]

# ============================================================
#  НИЖЕ ТЕХНИЧЕСКАЯ ЧАСТЬ. ОБЫЧНО ЕЁ МЕНЯТЬ НЕ НУЖНО.
# ============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TZ = ZoneInfo(TIMEZONE)
SERVICES_BY_ID = {s["id"]: s for s in SERVICES}
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

WELCOME_TEXT = (
    f"Здравствуйте! 💅\nЭто бот мастера {MASTER_NAME}, {BUSINESS_NAME}.\n"
    "Здесь можно записаться на маникюр или педикюр, посмотреть цены и свои записи."
)

router = Router()


# ---------- Время и даты ----------
def hm_to_min(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def min_to_hm(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


WORK_START_MIN = hm_to_min(WORK_START)
WORK_END_MIN = hm_to_min(WORK_END)


def now() -> datetime:
    return datetime.now(TZ)


def regular_ranges(d: date) -> list[tuple[int, int]]:
    """Обычный график мастера на день (пусто в выходной или в режиме «только окна мастера»)."""
    if OPEN_ONLY_BY_ADMIN:
        return []
    if d.weekday() in WORK_DAYS and d.isoformat() not in DAYS_OFF:
        return [(WORK_START_MIN, WORK_END_MIN)]
    return []


def open_ranges(d: date) -> list[tuple[int, int]]:
    """Все промежутки, куда можно записываться: обычный график + окна, добавленные мастером."""
    return regular_ranges(d) + windows_for(d.isoformat())


def available_dates() -> list[date]:
    today = now().date()
    result = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        if open_ranges(d):
            result.append(d)
    return result


def fmt_button_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"


def fmt_full_date(iso_date: str) -> str:
    return date.fromisoformat(iso_date).strftime("%d.%m.%Y")


def fmt_duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


def day_word(d: date) -> str:
    today = now().date()
    if d == today:
        return "сегодня"
    if d == today + timedelta(days=1):
        return "завтра"
    return d.strftime("%d.%m.%Y")


def booking_start(row) -> datetime:
    start = row["start_min"]
    return datetime.combine(date.fromisoformat(row["date"]), dtime(start // 60, start % 60), tzinfo=TZ)


def time_range(row) -> str:
    return f"{min_to_hm(row['start_min'])}–{min_to_hm(row['start_min'] + row['minutes'])}"


# ---------- База данных (SQLite, файл manicure.db) ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                service TEXT NOT NULL,
                date TEXT NOT NULL,
                start_min INTEGER NOT NULL,
                minutes INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                reminded INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS windows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                start_min INTEGER NOT NULL,
                end_min INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def is_free(start: int, duration: int, booked: list[tuple[int, int]]) -> bool:
    """Проверяет, не пересекается ли новая запись с уже существующими (с учётом перерыва)."""
    end = start + duration
    for b_start, b_end in booked:
        if start < b_end + BUFFER_MINUTES and b_start < end + BUFFER_MINUTES:
            return False
    return True


def booked_intervals(iso_date: str) -> list[tuple[int, int]]:
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT start_min, minutes FROM bookings WHERE date = ?", (iso_date,)
        ).fetchall()
    return [(r["start_min"], r["start_min"] + r["minutes"]) for r in rows]


def free_slots(d: date, duration: int) -> list[int]:
    """Список свободных времён начала (в минутах от полуночи) для услуги данной длительности."""
    booked = booked_intervals(d.isoformat())
    current = now()
    now_min = current.hour * 60 + current.minute
    candidates = set()
    for range_start, range_end in open_ranges(d):
        for start in range(range_start, range_end - duration + 1, SLOT_STEP_MINUTES):
            candidates.add(start)
    slots = []
    for start in sorted(candidates):
        if d == current.date() and start < now_min + MIN_NOTICE_MINUTES:
            continue
        if is_free(start, duration, booked):
            slots.append(start)
    return slots


def add_booking(user_id, username, name, phone, service_name, iso_date, start, duration):
    """Создаёт запись. Возвращает id записи или None, если время уже занято."""
    start_dt = datetime.combine(
        date.fromisoformat(iso_date), dtime(start // 60, start % 60), tzinfo=TZ
    )
    # Если запись сделана меньше чем за REMINDER_HOURS до визита, напоминание не нужно.
    reminded = 1 if start_dt - now() <= timedelta(hours=REMINDER_HOURS) else 0
    with closing(db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT start_min, minutes FROM bookings WHERE date = ?", (iso_date,)
        ).fetchall()
        booked = [(r["start_min"], r["start_min"] + r["minutes"]) for r in rows]
        if not is_free(start, duration, booked):
            conn.rollback()
            return None
        cursor = conn.execute(
            "INSERT INTO bookings (user_id, username, name, phone, service, date, start_min, "
            "minutes, created_at, reminded) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, name, phone, service_name, iso_date, start, duration,
             now().isoformat(), reminded),
        )
        conn.commit()
        return cursor.lastrowid


def get_booking(booking_id: int, user_id: int):
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM bookings WHERE id = ? AND user_id = ?", (booking_id, user_id)
        ).fetchone()


def delete_booking(booking_id: int) -> None:
    with closing(db()) as conn:
        conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        conn.commit()


def mark_reminded(booking_id: int) -> None:
    with closing(db()) as conn:
        conn.execute("UPDATE bookings SET reminded = 1 WHERE id = ?", (booking_id,))
        conn.commit()


def upcoming_bookings(user_id: int | None = None, on_date: str | None = None, limit: int = 100):
    sql = "SELECT * FROM bookings WHERE date >= ?"
    params: list = [now().date().isoformat()]
    if on_date is not None:
        sql += " AND date = ?"
        params.append(on_date)
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    sql += " ORDER BY date, start_min LIMIT ?"
    params.append(limit)
    with closing(db()) as conn:
        return conn.execute(sql, params).fetchall()


# ---------- Свободные окна, которые добавляет мастер ----------
def windows_for(iso_date: str) -> list[tuple[int, int]]:
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT start_min, end_min FROM windows WHERE date = ?", (iso_date,)
        ).fetchall()
    return [(r["start_min"], r["end_min"]) for r in rows]


def add_window(iso_date: str, start: int, end: int) -> bool:
    """Добавляет окно. Возвращает False, если точно такое окно уже есть."""
    with closing(db()) as conn:
        exists = conn.execute(
            "SELECT 1 FROM windows WHERE date = ? AND start_min = ? AND end_min = ?",
            (iso_date, start, end),
        ).fetchone()
        if exists:
            return False
        conn.execute(
            "INSERT INTO windows (date, start_min, end_min, created_at) VALUES (?, ?, ?, ?)",
            (iso_date, start, end, now().isoformat()),
        )
        conn.commit()
    return True


def list_windows(limit: int = 100):
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM windows WHERE date >= ? ORDER BY date, start_min LIMIT ?",
            (now().date().isoformat(), limit),
        ).fetchall()


def delete_window(window_id: int) -> bool:
    with closing(db()) as conn:
        cursor = conn.execute("DELETE FROM windows WHERE id = ?", (window_id,))
        conn.commit()
        return cursor.rowcount > 0


TIME_RANGE_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})\s*$")


def parse_ranges(text: str) -> list[tuple[int, int]] | None:
    """Разбирает «15:00-18:00» или «10:00-12:00, 15:00-18:00». Возвращает None при ошибке."""
    result = []
    for part in re.split(r"[,;\n]+", text):
        if not part.strip():
            continue
        match = TIME_RANGE_RE.match(part)
        if match is None:
            return None
        h1, m1, h2, m2 = (int(x) for x in match.groups())
        if h1 > 23 or m1 > 59 or h2 > 24 or m2 > 59:
            return None
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if start >= end or end > 24 * 60:
            return None
        result.append((start, end))
    return result or None


# ---------- Клавиатуры ----------
def main_menu(user_id: int | None = None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📅 Записаться")],
        [KeyboardButton(text="💰 Услуги и цены"), KeyboardButton(text="📋 Мои записи")],
        [KeyboardButton(text="❓ Частые вопросы"), KeyboardButton(text="📍 Контакты")],
    ]
    if PORTFOLIO_URL:
        rows.insert(1, [KeyboardButton(text="💅 Примеры работ")])
    if user_id is not None and user_id in ADMIN_IDS:
        rows.append([KeyboardButton(text="➕ Добавить окно"), KeyboardButton(text="🗓 Открытые окна")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def make_inline(buttons: list[InlineKeyboardButton], per_row: int, extra_rows=None):
    rows = [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]
    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def edit_or_send(callback: CallbackQuery, text: str, markup=None) -> None:
    """Редактирует сообщение с кнопкой. Если не вышло (старое сообщение), присылает новое."""
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except (TelegramBadRequest, AttributeError):
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as error:  # мастер мог не нажать /start у бота
            logging.warning("Не удалось отправить сообщение мастеру %s: %s", admin_id, error)


def is_admin(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in ADMIN_IDS


async def send_in_chunks(message: Message, blocks: list[str]) -> None:
    """Отправляет длинный список частями (в Telegram лимит 4096 символов)."""
    chunk = ""
    for block in blocks:
        if len(chunk) + len(block) + 2 > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += block + "\n\n"
    if chunk:
        await message.answer(chunk)


# ---------- Состояния записи ----------
class Booking(StatesGroup):
    service = State()
    day = State()
    slot = State()
    name = State()
    phone = State()


class AddWindow(StatesGroup):
    day = State()
    times = State()


# ============================================================
#  ОБРАБОТЧИКИ. Порядок важен: меню и команды идут раньше шагов записи.
# ============================================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=main_menu(message.from_user.id))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu(message.from_user.id))


@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(F.text == "💰 Услуги и цены")
async def show_services(message: Message, state: FSMContext):
    await state.clear()
    lines = [f"• {s['name']} — {s['price']} ₽ ({fmt_duration(s['minutes'])})" for s in SERVICES]
    await message.answer("<b>Услуги и цены</b>\n\n" + "\n".join(lines))


@router.message(F.text == "📍 Контакты")
async def show_contacts(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(CONTACTS)


@router.message(F.text == "💅 Примеры работ")
async def show_portfolio(message: Message, state: FSMContext):
    await state.clear()
    if not PORTFOLIO_URL:
        await message.answer("Примеры работ скоро появятся.")
        return
    markup = make_inline([InlineKeyboardButton(text="Смотреть работы", url=PORTFOLIO_URL)], 1)
    await message.answer("Мои работы можно посмотреть здесь 👇", reply_markup=markup)


@router.message(F.text == "❓ Частые вопросы")
async def show_faq(message: Message, state: FSMContext):
    await state.clear()
    buttons = [
        InlineKeyboardButton(text=question, callback_data=f"faq:{index}")
        for index, (question, _answer) in enumerate(FAQ)
    ]
    await message.answer("Выберите вопрос:", reply_markup=make_inline(buttons, 1))


@router.callback_query(F.data.startswith("faq:"))
async def faq_answer(callback: CallbackQuery):
    try:
        index = int(callback.data.split(":", 1)[1])
        question, answer = FAQ[index]
    except (ValueError, IndexError):
        await callback.answer()
        return
    await callback.bot.send_message(callback.from_user.id, f"<b>{question}</b>\n\n{answer}")
    await callback.answer()


# ---------- Мои записи и отмена ----------
@router.message(F.text == "📋 Мои записи")
@router.message(Command("my"))
async def my_bookings(message: Message, state: FSMContext):
    await state.clear()
    rows = upcoming_bookings(user_id=message.from_user.id)
    if not rows:
        await message.answer("У вас нет предстоящих записей.")
        return
    for row in rows:
        markup = make_inline(
            [InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"cancel:{row['id']}")], 1
        )
        await message.answer(
            f"📌 <b>{html.escape(row['service'])}</b>\n"
            f"{fmt_full_date(row['date'])}, {time_range(row)}",
            reply_markup=markup,
        )


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_my_booking(callback: CallbackQuery):
    try:
        booking_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    row = get_booking(booking_id, callback.from_user.id)
    if row is None:
        await edit_or_send(callback, "Эта запись уже отменена.")
        await callback.answer()
        return
    if booking_start(row) - now() < timedelta(hours=CANCEL_BEFORE_HOURS):
        await callback.answer(
            f"Онлайн отменить можно не позднее чем за {CANCEL_BEFORE_HOURS} ч до визита. "
            f"Пожалуйста, позвоните мастеру: {MASTER_PHONE}",
            show_alert=True,
        )
        return
    delete_booking(booking_id)
    await edit_or_send(
        callback,
        f"❌ Запись отменена: {html.escape(row['service'])}, "
        f"{fmt_full_date(row['date'])}, {time_range(row)}",
    )
    await callback.answer()
    await notify_admins(
        callback.bot,
        "❌ <b>Клиент отменил запись</b>\n"
        f"{html.escape(row['service'])}, {fmt_full_date(row['date'])}, {time_range(row)}\n"
        f"{html.escape(row['name'])}, {html.escape(row['phone'])}",
    )


# ---------- Команды мастера ----------
def admin_line(row) -> str:
    username = f" @{html.escape(row['username'])}" if row["username"] else ""
    return (
        f"<b>{fmt_full_date(row['date'])} {time_range(row)}</b> — {html.escape(row['service'])}\n"
        f"{html.escape(row['name'])}, {html.escape(row['phone'])}{username}"
    )


@router.message(Command("today"))
async def admin_today(message: Message):
    if not is_admin(message):
        await message.answer("Эта команда доступна только мастеру.")
        return
    rows = upcoming_bookings(on_date=now().date().isoformat())
    if not rows:
        await message.answer("На сегодня записей нет.")
        return
    await send_in_chunks(message, [admin_line(r) for r in rows])


@router.message(Command("bookings"))
async def admin_bookings(message: Message):
    if not is_admin(message):
        await message.answer("Эта команда доступна только мастеру.")
        return
    rows = upcoming_bookings()
    if not rows:
        await message.answer("Предстоящих записей нет.")
        return
    await send_in_chunks(message, [admin_line(r) for r in rows])


# ---------- Мастер добавляет и удаляет свободные окна ----------
@router.message(F.text == "➕ Добавить окно")
@router.message(Command("window"))
async def start_add_window(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message):
        await message.answer("Эта команда доступна только мастеру.")
        return
    await state.set_state(AddWindow.day)
    today = now().date()
    days = [today + timedelta(days=i) for i in range(DAYS_AHEAD)]
    buttons = [
        InlineKeyboardButton(text=fmt_button_date(d), callback_data=f"wday:{d.isoformat()}")
        for d in days
    ]
    await message.answer(
        "На какую дату добавить свободное окно?", reply_markup=make_inline(buttons, 3)
    )


@router.callback_query(AddWindow.day, F.data.startswith("wday:"))
async def choose_window_day(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return
    try:
        chosen = date.fromisoformat(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    if chosen < now().date():
        await callback.answer("Эта дата уже прошла", show_alert=True)
        return
    await state.update_data(window_day=chosen.isoformat())
    await state.set_state(AddWindow.times)
    await edit_or_send(
        callback,
        f"Дата: <b>{fmt_button_date(chosen)}</b>\n\n"
        "Напишите время окна, например:\n<code>15:00-18:00</code>\n\n"
        "Можно несколько окон сразу, через запятую:\n<code>10:00-12:00, 15:00-18:00</code>\n\n"
        "Отмена: /cancel",
    )
    await callback.answer()


@router.message(F.text == "🗓 Открытые окна")
@router.message(Command("windows"))
async def list_open_windows(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message):
        await message.answer("Эта команда доступна только мастеру.")
        return
    rows = list_windows()
    if not rows:
        await message.answer(
            "Добавленных вами окон нет. Чтобы открыть время, нажмите «➕ Добавить окно»."
        )
        return
    for row in rows:
        markup = make_inline(
            [InlineKeyboardButton(text="🗑 Удалить окно", callback_data=f"wdel:{row['id']}")], 1
        )
        await message.answer(
            f"🗓 <b>{fmt_full_date(row['date'])}</b>, "
            f"{min_to_hm(row['start_min'])}–{min_to_hm(row['end_min'])}",
            reply_markup=markup,
        )


@router.callback_query(F.data.startswith("wdel:"))
async def delete_open_window(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return
    try:
        window_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    if delete_window(window_id):
        await edit_or_send(callback, "🗑 Окно удалено. Уже сделанные записи остаются в силе.")
    else:
        await edit_or_send(callback, "Это окно уже удалено.")
    await callback.answer()


# ---------- Запись: шаг 1, выбор услуги ----------
@router.message(F.text == "📅 Записаться")
@router.message(Command("book"))
async def start_booking(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Booking.service)
    buttons = [
        InlineKeyboardButton(
            text=f"{s['name']} — {s['price']} ₽ · {fmt_duration(s['minutes'])}",
            callback_data=f"svc:{s['id']}",
        )
        for s in SERVICES
    ]
    await message.answer("Выберите услугу:", reply_markup=make_inline(buttons, 1))


# ---------- Шаг 2, выбор даты ----------
async def render_dates(callback: CallbackQuery, state: FSMContext, service: dict) -> None:
    dates = available_dates()
    if not dates:
        await state.clear()
        await edit_or_send(
            callback,
            "К сожалению, сейчас нет свободных дат для записи. "
            f"Загляните позже или позвоните мастеру: {MASTER_PHONE}",
        )
        return
    await state.set_state(Booking.day)
    buttons = [
        InlineKeyboardButton(text=fmt_button_date(d), callback_data=f"day:{d.isoformat()}")
        for d in dates
    ]
    await edit_or_send(
        callback,
        f"Услуга: <b>{service['name']}</b>\n"
        f"{service['price']} ₽, {fmt_duration(service['minutes'])}\n\nВыберите дату:",
        make_inline(buttons, 3),
    )


@router.callback_query(Booking.service, F.data.startswith("svc:"))
async def choose_service(callback: CallbackQuery, state: FSMContext):
    service = SERVICES_BY_ID.get(callback.data.split(":", 1)[1])
    if service is None:
        await callback.answer("Услуга не найдена", show_alert=True)
        return
    await state.update_data(service_id=service["id"])
    await render_dates(callback, state, service)
    await callback.answer()


# ---------- Шаг 3, выбор времени ----------
@router.callback_query(Booking.day, F.data.startswith("day:"))
async def choose_day(callback: CallbackQuery, state: FSMContext):
    try:
        chosen = date.fromisoformat(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    if chosen not in available_dates():
        await callback.answer("Эта дата недоступна", show_alert=True)
        return
    data = await state.get_data()
    service = SERVICES_BY_ID[data["service_id"]]
    slots = free_slots(chosen, service["minutes"])
    if not slots:
        await callback.answer("На этот день нет свободного времени. Выберите другой.", show_alert=True)
        return
    await state.update_data(day=chosen.isoformat())
    await state.set_state(Booking.slot)
    buttons = [
        InlineKeyboardButton(text=min_to_hm(s), callback_data=f"slot:{s}") for s in slots
    ]
    back = [[InlineKeyboardButton(text="⬅️ Другая дата", callback_data="back:day")]]
    await edit_or_send(
        callback,
        f"Услуга: <b>{service['name']}</b>\n"
        f"Дата: <b>{fmt_full_date(chosen.isoformat())}</b>\n\nВыберите время начала:",
        make_inline(buttons, 4, extra_rows=back),
    )
    await callback.answer()


@router.callback_query(Booking.slot, F.data == "back:day")
async def back_to_days(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await render_dates(callback, state, SERVICES_BY_ID[data["service_id"]])
    await callback.answer()


# ---------- Шаг 4, имя ----------
@router.callback_query(Booking.slot, F.data.startswith("slot:"))
async def choose_slot(callback: CallbackQuery, state: FSMContext):
    try:
        start = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    data = await state.get_data()
    service = SERVICES_BY_ID[data["service_id"]]
    if start not in free_slots(date.fromisoformat(data["day"]), service["minutes"]):
        await callback.answer("Это время уже занято. Выберите другое.", show_alert=True)
        return
    await state.update_data(slot=start)
    await state.set_state(Booking.name)
    await edit_or_send(
        callback,
        f"Услуга: <b>{service['name']}</b>\n"
        f"Дата и время: <b>{fmt_full_date(data['day'])}, "
        f"{min_to_hm(start)}–{min_to_hm(start + service['minutes'])}</b>",
    )
    await callback.bot.send_message(
        callback.from_user.id,
        "Как вас зовут?\n(Чтобы отменить запись, отправьте /cancel)",
        reply_markup=ReplyKeyboardRemove(),
    )
    await callback.answer()


@router.message(Booking.name, F.text)
async def get_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 2 <= len(name) <= 50:
        await message.answer("Пожалуйста, введите имя (от 2 до 50 символов).")
        return
    await state.update_data(name=name)
    await state.set_state(Booking.phone)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Отправьте номер телефона кнопкой ниже или напишите его вручную.",
        reply_markup=keyboard,
    )


# ---------- Шаг 5, телефон и завершение ----------
async def finish_booking(message: Message, state: FSMContext, phone: str) -> None:
    data = await state.get_data()
    await state.clear()
    service = SERVICES_BY_ID[data["service_id"]]
    booking_id = add_booking(
        user_id=message.from_user.id,
        username=message.from_user.username,
        name=data["name"],
        phone=phone,
        service_name=service["name"],
        iso_date=data["day"],
        start=data["slot"],
        duration=service["minutes"],
    )
    if booking_id is None:
        await message.answer(
            "😔 Это время только что заняли. Нажмите «📅 Записаться» и выберите другое.",
            reply_markup=main_menu(message.from_user.id),
        )
        return
    when = (
        f"{fmt_full_date(data['day'])}, "
        f"{min_to_hm(data['slot'])}–{min_to_hm(data['slot'] + service['minutes'])}"
    )
    await message.answer(
        "✅ <b>Вы записаны!</b>\n\n"
        f"Услуга: {service['name']}\n"
        f"Когда: {when}\n"
        f"Стоимость: {service['price']} ₽\n"
        f"Адрес: {ADDRESS}\n\n"
        f"Перед визитом пришлю напоминание. Отменить запись можно в «📋 Мои записи» "
        f"не позднее чем за {CANCEL_BEFORE_HOURS} ч до визита.",
        reply_markup=main_menu(message.from_user.id),
    )
    username = (
        f"\nTelegram: @{html.escape(message.from_user.username)}"
        if message.from_user.username
        else ""
    )
    await notify_admins(
        message.bot,
        "🔔 <b>Новая запись</b>\n\n"
        f"Услуга: {service['name']}\n"
        f"Когда: {when}\n"
        f"Имя: {html.escape(data['name'])}\n"
        f"Телефон: {html.escape(phone)}{username}",
    )


@router.message(Booking.phone, F.contact)
async def phone_from_contact(message: Message, state: FSMContext):
    await finish_booking(message, state, message.contact.phone_number)


@router.message(Booking.phone, F.text)
async def phone_from_text(message: Message, state: FSMContext):
    phone = message.text.strip()
    digits = re.sub(r"\D", "", phone)
    if not 10 <= len(digits) <= 15:
        await message.answer("Не похоже на номер телефона. Пример: +7 900 123-45-67")
        return
    await finish_booking(message, state, phone)


# ---------- Мастер вводит время окна (после всех меню, чтобы кнопки меню работали) ----------
@router.message(AddWindow.times, F.text)
async def add_window_times(message: Message, state: FSMContext):
    if not is_admin(message):
        await state.clear()
        return
    ranges = parse_ranges(message.text)
    if ranges is None:
        await message.answer(
            "Не получилось разобрать время. Напишите так: <code>15:00-18:00</code> "
            "или несколько окон через запятую: <code>10:00-12:00, 15:00-18:00</code>"
        )
        return
    shortest = min(s["minutes"] for s in SERVICES)
    if any(end - start < shortest for start, end in ranges):
        await message.answer(
            f"Окно слишком короткое: самая короткая услуга длится {fmt_duration(shortest)}. "
            "Напишите время ещё раз."
        )
        return
    data = await state.get_data()
    day_iso = data["window_day"]
    await state.clear()
    added = [(start, end) for start, end in ranges if add_window(day_iso, start, end)]
    if not added:
        await message.answer(
            "Такие окна уже добавлены.", reply_markup=main_menu(message.from_user.id)
        )
        return
    times = ", ".join(f"{min_to_hm(start)}–{min_to_hm(end)}" for start, end in added)
    await message.answer(
        f"✅ Окно добавлено: {fmt_full_date(day_iso)}, {times}\n"
        "Клиенты уже могут записаться на это время.",
        reply_markup=main_menu(message.from_user.id),
    )


# ---------- Всё остальное ----------
@router.callback_query()
async def stale_button(callback: CallbackQuery):
    await callback.answer("Эта кнопка устарела. Нажмите «📅 Записаться» ещё раз.", show_alert=True)


@router.message()
async def fallback(message: Message):
    await message.answer(
        "Не совсем понял вас 🙂 Выберите действие в меню или нажмите /start.",
        reply_markup=main_menu(message.from_user.id),
    )


# ============================================================
#  НАПОМИНАНИЯ КЛИЕНТАМ
# ============================================================
async def send_due_reminders(bot: Bot) -> None:
    today = now().date().isoformat()
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE reminded = 0 AND date >= ?", (today,)
        ).fetchall()
    current = now()
    for row in rows:
        start = booking_start(row)
        if start <= current:
            mark_reminded(row["id"])
            continue
        if start - current > timedelta(hours=REMINDER_HOURS):
            continue
        markup = make_inline(
            [InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"cancel:{row['id']}")], 1
        )
        text = (
            "⏰ <b>Напоминание о записи</b>\n\n"
            f"{html.escape(row['service'])}\n"
            f"{day_word(start.date())}, {time_range(row)}\n"
            f"{ADDRESS}\n\n"
            "Если не получится прийти, отмените запись кнопкой ниже."
        )
        try:
            await bot.send_message(row["user_id"], text, reply_markup=markup)
        except Exception as error:
            logging.warning("Не удалось отправить напоминание %s: %s", row["id"], error)
        mark_reminded(row["id"])


async def reminder_loop(bot: Bot) -> None:
    while True:
        try:
            await send_due_reminders(bot)
        except Exception:
            logging.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(60)


# ============================================================
#  ЗАПУСК
# ============================================================
async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не найден BOT_TOKEN. Заполните файл .env (инструкция в README.md).")
    init_db()
    session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="book", description="Записаться"),
            BotCommand(command="my", description="Мои записи"),
            BotCommand(command="cancel", description="Отменить текущее действие"),
        ]
    )
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен: %s", BUSINESS_NAME)
    reminders = asyncio.create_task(reminder_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminders.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен")
