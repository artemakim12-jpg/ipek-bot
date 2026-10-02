import asyncio
import json
import logging
import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import quote
from calendar import monthrange

from vkbottle.bot import Bot, Message
from vkbottle import Keyboard, KeyboardButtonColor, Text

# ============================================================
# КОНФИГУРАЦИЯ (берётся из переменных окружения Bothost)
# ============================================================
VK_TOKEN = os.getenv("VK_TOKEN")
VK_GROUP_ID = int(os.getenv("VK_GROUP_ID", "0"))

TARGET_GROUP = "И-25-1"
TARGET_COURSE = "2 курс"
CLOUD_URL = "https://cloud.pilot-ipek.ru/s/7HDT8FZGBd7J5cs"

BASE_URL = "https://www.pilot-ipek.ru"
API_URL = f"{BASE_URL}/api/get_list"

CHECK_INTERVAL = 300
DAYS_AHEAD = 7
CACHE_FILE = "schedule_cache.json"

MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}
MONTHS_NOM_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}
MONTH_NUM_BY_NAME = {name: num for num, name in MONTHS_NOM_RU.items()}
DATES_PER_PAGE = 3
MONTHS_PER_PAGE = 6

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=VK_TOKEN)
last_cloud_signature = None
loop = None
user_state = {}
schedule_snapshot = {}


def load_cache():
    global schedule_snapshot, last_cloud_signature
    if not os.path.exists(CACHE_FILE):
        logger.info("Файл кэша не найден — начинаем с нуля.")
        return
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        schedule_snapshot = data.get("schedule", {})
        last_cloud_signature = data.get("cloud_signature")
        logger.info(f"Кэш загружен: {len(schedule_snapshot)} дней.")
    except Exception as e:
        logger.error(f"Ошибка загрузки кэша: {e}")


def save_cache():
    try:
        data = {"schedule": schedule_snapshot, "cloud_signature": last_cloud_signature}
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения кэша: {e}")


def get_last_day_of_month(month_name, year=None):
    if year is None:
        year = datetime.now().year
    m = MONTH_NUM_BY_NAME.get(month_name)
    return monthrange(year, m)[1] if m else 31


def extract_days_from_text(t):
    return [int(x) for x in re.findall(r"\b(\d{1,2})\b", t)]


def extract_month_from_text(t):
    tl = t.lower()
    for n, name in MONTHS_RU.items():
        if name in tl:
            return n
    return None


def get_cloud_signature():
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = requests.get(CLOUD_URL, headers=h, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Облако недоступно: {e}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    parts = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        n = cells[0].get_text(" ", strip=True)
        d = cells[-1].get_text(" ", strip=True)
        if n and d:
            parts.append(f"{n}|{d}")
    if not parts:
        parts.append(soup.get_text(" ", strip=True)[:2000])
    return "\n".join(parts)


def check_cloud_and_notify():
    global last_cloud_signature
    sig = get_cloud_signature()
    if not sig:
        return
    if last_cloud_signature is None:
        last_cloud_signature = sig
        save_cache()
        return
    if sig != last_cloud_signature:
        last_cloud_signature = sig
        save_cache()
        msg = (
            "🔔 Новые материалы в дистанционном обучении!\n\n"
            f"📚 Группа {TARGET_GROUP} · {TARGET_COURSE}\n"
            f"🔗 {CLOUD_URL}\n\n"
            f"Откройте: {TARGET_COURSE} → {TARGET_GROUP}"
        )
        try:
            asyncio.run_coroutine_threadsafe(
                bot.api.messages.send(peer_id=VK_GROUP_ID, message=msg, random_id=0),
                loop,
            )
            logger.info("Уведомление об облаке отправлено.")
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления: {e}")
    else:
        logger.info("Облако без изменений.")


def get_api_data():
    try:
        r = requests.get(API_URL, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API недоступно: {e}")
        return {}


def get_all_date_links():
    data = get_api_data()
    out = []
    for mk, dates in data.items():
        if not isinstance(dates, list):
            continue
        if mk in ("Текущий месяц", "Расписание звонков"):
            continue
        for dt in dates:
            out.append((dt, f"{BASE_URL}/raspo/{quote(dt)}"))
    return out


def get_months_with_dates():
    data = get_api_data()
    out = []
    for n in range(1, 13):
        name = MONTHS_NOM_RU[n]
        ds = data.get(name, [])
        if isinstance(ds, list) and ds:
            out.append((name, ds))
    return out


def find_link_for_date(target):
    day = target.day
    mn = MONTHS_RU[target.month]
    for text, url in get_all_date_links():
        tl = text.lower()
        if mn not in tl:
            continue
        if re.search(rf"(?<!\d)0?{day}(?!\d)", tl):
            return text, url
    return None, None


def get_latest_available_date():
    latest = None
    for dt, _ in get_all_date_links():
        days = extract_days_from_text(dt)
        mn = extract_month_from_text(dt)
        if not days or not mn:
            continue
        for d in days:
            try:
                dt2 = datetime(datetime.now().year, mn, d)
            except ValueError:
                continue
            if latest is None or dt2 > latest:
                latest = dt2
    return latest


def get_day_from_text(t):
    m = re.match(r"^(\d{1,2})", t.strip())
    return int(m.group(1)) if m else None


def filter_dates_by_range(dates, s, e):
    out = []
    for d in dates:
        day = get_day_from_text(d)
        if day is None:
            continue
        if e == 0:
            if s <= day <= 31:
                out.append(d)
        else:
            if s <= day <= e:
                out.append(d)
    out.sort(key=lambda x: get_day_from_text(x) or 0)
    return out


def get_available_ranges(month_name, dates):
    last = get_last_day_of_month(month_name)
    ranges = [
        ("1–10", filter_dates_by_range(dates, 1, 10)),
        ("11–20", filter_dates_by_range(dates, 11, 20)),
        (f"21–{last}", filter_dates_by_range(dates, 21, 0)),
    ]
    return [(l, i) for l, i in ranges if i]


def filter_by_range_label(dates, label):
    if label == "1–10":
        return filter_dates_by_range(dates, 1, 10)
    if label == "11–20":
        return filter_dates_by_range(dates, 11, 20)
    if label.startswith("21–"):
        return filter_dates_by_range(dates, 21, 0)
    return []


def clean_cell_text(cell):
    ps = cell.find_all("p")
    if not ps:
        t = cell.get_text(" ", strip=True)
        return t if t else "—"
    parts = [p.get_text(" ", strip=True) for p in ps]
    parts = [p for p in parts if p and p.strip() != "–"]
    if not parts:
        return "—"
    return " / ".join(parts)


def parse_all_tables_from_url(url):
    h = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=h, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Не открыть {url}: {e}")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for w in soup.find_all("div", class_="table-wrapper"):
        table = w.find("table")
        if not table:
            continue
        rows = table.find_all("tr")
        if not rows:
            continue
        hc = rows[0].find_all(["td", "th"])
        col = None
        for i, c in enumerate(hc):
            if TARGET_GROUP in c.get_text(strip=True):
                col = i
                break
        if col is None:
            continue
        tt = w.find_previous(["p", "h1", "h2", "h3"])
        title = tt.get_text(" ", strip=True) if tt else ""
        lessons = []
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= col:
                continue
            tc = cells[0].get_text(" ", strip=True)
            if "пара" not in tc.lower():
                continue
            lessons.append((tc, clean_cell_text(cells[col])))
        if lessons:
            out.append({"title": title, "lessons": lessons})
    return out


def parse_schedule_for_date(target):
    _, url = find_link_for_date(target)
    if not url:
        return []
    tables = parse_all_tables_from_url(url)
    day = target.day
    mn = MONTHS_RU[target.month]
    flex = r"[\s\u00A0\u2009\u202F]+"
    pat = re.compile(rf"\b0?{day}{flex}{mn}{flex}\d{{4}}\b", re.IGNORECASE)
    return [t for t in tables if pat.search(t["title"])]


def parse_schedule_for_date_text(dt_text):
    days = extract_days_from_text(dt_text)
    mn = extract_month_from_text(dt_text)
    if not days or not mn:
        return []
    url = None
    for text, u in get_all_date_links():
        if text.lower() == dt_text.lower():
            url = u
            break
    if not url:
        return []
    tables = parse_all_tables_from_url(url)
    if not tables:
        return []
    flex = r"[\s\u00A0\u2009\u202F]+"
    mn_name = MONTHS_RU[mn]
    pats = [re.compile(rf"\b0?{d}{flex}{mn_name}{flex}\d{{4}}\b", re.IGNORECASE) for d in days]
    matched = []
    for t in tables:
        for p in pats:
            if p.search(t["title"]):
                matched.append(t)
                break
    return matched if matched else tables


def make_snapshot_for_day(target):
    tables = parse_schedule_for_date(target)
    if not tables:
        return []
    lessons = tables[0]["lessons"]
    result = []
    for ti, content in lessons:
        t = re.sub(r"\s+", " ", ti).strip()
        c = re.sub(r"\s+", " ", content).strip()
        result.append(f"{t}|{c}")
    return result


def parse_date_key(key):
    try:
        return datetime.strptime(key, "%d.%m.%Y")
    except ValueError:
        return None


def build_new_snapshot():
    snapshot = {}
    today = datetime.now()
    for i in range(DAYS_AHEAD):
        target = today + timedelta(days=i)
        key = target.strftime("%d.%m.%Y")
        lessons = make_snapshot_for_day(target)
        if lessons:
            snapshot[key] = lessons
    return snapshot


def format_change_message(changes):
    lines = ["⚠️ Изменения в расписании!", ""]
    for change in changes:
        lines.append(change)
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n… (обрезано)"
    return msg


def diff_snapshots(old, new):
    changes = []
    all_keys = set(old.keys()) | set(new.keys())
    for key in sorted(all_keys, key=lambda k: parse_date_key(k) or datetime.min):
        old_l = old.get(key, [])
        new_l = new.get(key, [])
        if not old_l and new_l:
            changes.append(f"📆 {key} — появилось новое расписание")
            for les in new_l:
                changes.append(f"   ✅ {les}")
            continue
        if old_l and not new_l:
            changes.append(f"📆 {key} — расписание удалено")
            continue
        old_s = set(old_l)
        new_s = set(new_l)
        if old_s == new_s:
            continue
        added = new_s - old_s
        removed = old_s - new_s
        if added or removed:
            changes.append(f"📆 {key}")
            for r in sorted(removed):
                changes.append(f"   ❌ Было: {r}")
            for a in sorted(added):
                changes.append(f"   ✅ Стало: {a}")
    return changes


def check_schedule_changes():
    global schedule_snapshot
    logger.info("Проверка расписания на изменения...")
    new_snap = build_new_snapshot()
    if not schedule_snapshot:
        schedule_snapshot = new_snap
        save_cache()
        logger.info(f"Первый слепок сохранён: {len(new_snap)} дней.")
        return
    changes = diff_snapshots(schedule_snapshot, new_snap)
    schedule_snapshot = new_snap
    save_cache()
    if not changes:
        logger.info("Изменений нет.")
        return
    logger.info(f"Найдено изменений: {len(changes)} строк. Отправляем...")
    msg = format_change_message(changes)
    try:
        asyncio.run_coroutine_threadsafe(
            bot.api.messages.send(peer_id=VK_GROUP_ID, message=msg, random_id=0),
            loop,
        )
        logger.info("Уведомление об изменениях отправлено.")
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")


def format_single_table(title, lessons):
    lines = ["━━━━━━━━━━━━━━━━━", f"📅 Расписание · {TARGET_GROUP}"]
    ds = ""
    if title:
        m = re.search(r"НА\s+(.+?года\s*\([^)]+\))", title, re.IGNORECASE)
        ds = m.group(1).strip() if m else title.strip()
    if ds:
        lines.append(f"📆 {ds}")
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append("")
    for ti, content in lessons:
        t = re.sub(r"\s+", " ", ti).strip().replace(".", ":")
        t = re.sub(r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})", r"\1 – \2", t)
        lines.append(f"🔵 {t}")
        parts = [p.strip() for p in content.split(" / ") if p.strip()]
        for i, part in enumerate(parts):
            if i == 0:
                lines.append(f"   📚 {part}")
            elif i == len(parts) - 1 and len(parts) > 1:
                lines.append(f"   🚪 {part}")
            else:
                lines.append(f"   👤 {part}")
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def format_multiple_tables(tables):
    msg = "\n\n".join(format_single_table(t["title"], t["lessons"]) for t in tables)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n… (обрезано)"
    return msg


def format_distance_message():
    return (
        "🎓 Дистанционное обучение\n\n"
        f"📚 Материалы для группы {TARGET_GROUP} ({TARGET_COURSE})\n\n"
        f"🔗 {CLOUD_URL}\n\n"
        f"Что делать:\n"
        f"1. Нажмите «{TARGET_COURSE}»\n"
        f"2. Затем «{TARGET_GROUP}»\n"
        f"3. Внутри — файлы и задания"
    )


def get_main_keyboard():
    k = Keyboard(one_time=False)
    k.add(Text("📅 Календарь"), color=KeyboardButtonColor.PRIMARY)
    k.add(Text("📆 Сегодня"), color=KeyboardButtonColor.SECONDARY)
    k.add(Text("📆 Завтра"), color=KeyboardButtonColor.SECONDARY)
    k.row()
    k.add(Text("🎓 Дистанционное обучение"), color=KeyboardButtonColor.POSITIVE)
    return k


def get_distance_keyboard():
    k = Keyboard(one_time=False)
    k.add(Text("🏠 В меню"), color=KeyboardButtonColor.SECONDARY)
    return k


def get_months_keyboard(months, page=1):
    total = (len(months) + MONTHS_PER_PAGE - 1) // MONTHS_PER_PAGE
    start = (page - 1) * MONTHS_PER_PAGE
    pm = months[start:start + MONTHS_PER_PAGE]
    k = Keyboard(one_time=False)
    cur = MONTHS_NOM_RU[datetime.now().month]
    for i, (name, _) in enumerate(pm):
        color = KeyboardButtonColor.PRIMARY if name == cur else KeyboardButtonColor.SECONDARY
        k.add(Text(name), color=color)
        if i % 2 == 1:
            k.row()
    if len(pm) % 2 == 1:
        k.row()
    if page > 1:
        k.add(Text(f"⬅ Месяцы: {page - 1}"), color=KeyboardButtonColor.PRIMARY)
    if page < total:
        k.add(Text(f"➡ Месяцы: {page + 1}"), color=KeyboardButtonColor.PRIMARY)
    if page > 1 or page < total:
        k.row()
    k.add(Text("🏠 В меню"), color=KeyboardButtonColor.SECONDARY)
    return k


def get_ranges_keyboard(month_name, ranges):
    k = Keyboard(one_time=False)
    for label, _ in ranges:
        k.add(Text(f"📅 {label} {month_name}"), color=KeyboardButtonColor.PRIMARY)
        k.row()
    k.add(Text("⬅ К месяцам"), color=KeyboardButtonColor.SECONDARY)
    k.add(Text("🏠 В меню"), color=KeyboardButtonColor.SECONDARY)
    return k


def get_days_keyboard(month_name, range_label, dates, page=1):
    total = (len(dates) + DATES_PER_PAGE - 1) // DATES_PER_PAGE
    start = (page - 1) * DATES_PER_PAGE
    pd = dates[start:start + DATES_PER_PAGE]
    k = Keyboard(one_time=False)
    for d in pd:
        k.add(Text(d), color=KeyboardButtonColor.SECONDARY)
        k.row()
    hp = page > 1
    hn = page < total
    if hp:
        k.add(Text("⬅ Назад"), color=KeyboardButtonColor.PRIMARY)
    if hn:
        k.add(Text("➡ Дальше"), color=KeyboardButtonColor.PRIMARY)
    if hp or hn:
        k.row()
    k.add(Text(f"📅 Диапазоны {month_name}"), color=KeyboardButtonColor.SECONDARY)
    k.add(Text("🏠 В меню"), color=KeyboardButtonColor.SECONDARY)
    return k


def is_month_button(m: Message) -> bool:
    return bool(m.text) and m.text.strip() in MONTHS_NOM_RU.values()


def is_months_pagination(m: Message) -> bool:
    if not m.text:
        return False
    t = m.text.strip()
    return t.startswith("⬅ Месяцы: ") or t.startswith("➡ Месяцы: ")


def is_range_button(m: Message) -> bool:
    if not m.text:
        return False
    t = m.text.strip()
    return t.startswith("📅 ") and re.match(r"^📅\s+\d+–\d+\s+\w+", t) is not None


def is_date_button(m: Message) -> bool:
    if not m.text:
        return False
    t = m.text.strip().lower()
    if not re.match(r"^\d{1,2}", t):
        return False
    months = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")
    return any(mm in t for mm in months)


def is_simple_pagination(m: Message) -> bool:
    return bool(m.text) and m.text.strip() in ("➡ Дальше", "⬅ Назад")


def is_back_to_ranges_button(m: Message) -> bool:
    return bool(m.text) and m.text.strip().startswith("📅 Диапазоны ")


@bot.on.message(text=["начать", "start", "привет", "/start", "🏠 В меню"])
async def start_handler(message: Message):
    await message.answer(
        f"👋 Привет! Я слежу за расписанием группы {TARGET_GROUP}.",
        keyboard=get_main_keyboard(),
    )


@bot.on.message(text="📅 Календарь")
async def calendar_handler(message: Message):
    months = await asyncio.to_thread(get_months_with_dates)
    if not months:
        await message.answer("📭 Нет данных с сайта.", keyboard=get_main_keyboard())
        return
    total = (len(months) + MONTHS_PER_PAGE - 1) // MONTHS_PER_PAGE
    await message.answer(f"📅 Выберите месяц (страница 1 из {total}):",
                         keyboard=get_months_keyboard(months, page=1))


@bot.on.message(func=is_months_pagination)
async def months_pagination_handler(message: Message):
    t = message.text.strip()
    if t.startswith("➡ Месяцы: "):
        rest = t[len("➡ Месяцы: "):]
    elif t.startswith("⬅ Месяцы: "):
        rest = t[len("⬅ Месяцы: "):]
    else:
        return
    try:
        page = int(rest)
    except ValueError:
        await message.answer("Ошибка.", keyboard=get_main_keyboard())
        return
    months = await asyncio.to_thread(get_months_with_dates)
    total = (len(months) + MONTHS_PER_PAGE - 1) // MONTHS_PER_PAGE
    if page < 1 or page > total:
        await message.answer("Такой страницы нет.", keyboard=get_main_keyboard())
        return
    await message.answer(f"📅 Выберите месяц (страница {page} из {total}):",
                         keyboard=get_months_keyboard(months, page=page))


@bot.on.message(func=is_month_button)
async def month_handler(message: Message):
    mn = message.text.strip()
    data = await asyncio.to_thread(get_api_data)
    dates = data.get(mn, [])
    if not isinstance(dates, list) or not dates:
        await message.answer(f"📭 В {mn} нет расписания.", keyboard=get_main_keyboard())
        return
    ranges = get_available_ranges(mn, dates)
    if not ranges:
        await message.answer(f"📭 В {mn} нет дат.", keyboard=get_main_keyboard())
        return
    await message.answer(f"📅 {mn}. Выберите диапазон:",
                         keyboard=get_ranges_keyboard(mn, ranges))


@bot.on.message(text="⬅ К месяцам")
async def back_to_months_handler(message: Message):
    months = await asyncio.to_thread(get_months_with_dates)
    if not months:
        await message.answer("Нет данных.", keyboard=get_main_keyboard())
        return
    total = (len(months) + MONTHS_PER_PAGE - 1) // MONTHS_PER_PAGE
    await message.answer(f"📅 Выберите месяц (страница 1 из {total}):",
                         keyboard=get_months_keyboard(months, page=1))


@bot.on.message(func=is_range_button)
async def range_handler(message: Message):
    rest = message.text.strip()[len("📅 "):]
    parts = rest.split(" ", 1)
    if len(parts) != 2:
        await message.answer("Ошибка.", keyboard=get_main_keyboard())
        return
    rl, mn = parts[0], parts[1]
    data = await asyncio.to_thread(get_api_data)
    dates = data.get(mn, [])
    if not isinstance(dates, list) or not dates:
        await message.answer(f"📭 В {mn} нет расписания.", keyboard=get_main_keyboard())
        return
    filtered = filter_by_range_label(dates, rl)
    if not filtered:
        await message.answer("📭 В этом диапазоне нет дат.", keyboard=get_main_keyboard())
        return
    user_state[message.peer_id] = {"month": mn, "range": rl, "page": 1}
    total = (len(filtered) + DATES_PER_PAGE - 1) // DATES_PER_PAGE
    await message.answer(
        f"📅 {mn}, {rl} — страница 1 из {total}.\nВыберите день:",
        keyboard=get_days_keyboard(mn, rl, filtered, page=1),
    )


@bot.on.message(func=is_back_to_ranges_button)
async def back_to_ranges_handler(message: Message):
    mn = message.text.strip()[len("📅 Диапазоны "):].strip()
    data = await asyncio.to_thread(get_api_data)
    dates = data.get(mn, [])
    if not isinstance(dates, list) or not dates:
        await message.answer(f"📭 В {mn} нет расписания.", keyboard=get_main_keyboard())
        return
    ranges = get_available_ranges(mn, dates)
    await message.answer(f"📅 {mn}. Выберите диапазон:",
                         keyboard=get_ranges_keyboard(mn, ranges))


@bot.on.message(func=is_simple_pagination)
async def simple_pagination_handler(message: Message):
    pid = message.peer_id
    st = user_state.get(pid)
    if not st:
        await message.answer("Начните с «📅 Календарь».", keyboard=get_main_keyboard())
        return
    mn = st["month"]
    rl = st["range"]
    page = st["page"]
    page += 1 if message.text.strip() == "➡ Дальше" else -1
    data = await asyncio.to_thread(get_api_data)
    dates = data.get(mn, [])
    filtered = filter_by_range_label(dates, rl)
    total = (len(filtered) + DATES_PER_PAGE - 1) // DATES_PER_PAGE
    if page < 1 or page > total:
        page = st["page"]
        await message.answer("Больше страниц нет.", keyboard=get_main_keyboard())
        return
    user_state[pid]["page"] = page
    await message.answer(
        f"📅 {mn}, {rl} — страница {page} из {total}.\nВыберите день:",
        keyboard=get_days_keyboard(mn, rl, filtered, page=page),
    )


@bot.on.message(func=is_date_button)
async def date_button_handler(message: Message):
    dt = message.text.strip()
    tables = await asyncio.to_thread(parse_schedule_for_date_text, dt)
    if not tables:
        await message.answer(f"📭 На «{dt}» нет расписания.", keyboard=get_main_keyboard())
        return
    await message.answer(format_multiple_tables(tables), keyboard=get_main_keyboard())


@bot.on.message(text="📆 Сегодня")
async def today_handler(message: Message):
    target = datetime.now()
    tables = await asyncio.to_thread(parse_schedule_for_date, target)
    if not tables:
        await message.answer("📭 На сегодня нет расписания.", keyboard=get_main_keyboard())
        return
    await message.answer(format_multiple_tables(tables), keyboard=get_main_keyboard())


@bot.on.message(text="📆 Завтра")
async def tomorrow_handler(message: Message):
    target = datetime.now() + timedelta(days=1)
    tables = await asyncio.to_thread(parse_schedule_for_date, target)
    if not tables:
        await message.answer("📭 На завтра нет расписания.", keyboard=get_main_keyboard())
        return
    await message.answer(format_multiple_tables(tables), keyboard=get_main_keyboard())


@bot.on.message(text="🎓 Дистанционное обучение")
async def distance_learning_handler(message: Message):
    await message.answer(format_distance_message(), keyboard=get_distance_keyboard())


def parse_date_text(text):
    t = text.strip().lower()
    m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else datetime.now().year
        if y < 100:
            y += 2000
        try:
            return datetime(y, mo, d)
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})\s+([а-яё]+)$", t)
    if m:
        d = int(m.group(1))
        w = m.group(2)
        for num, name in MONTHS_RU.items():
            if name.startswith(w[:3]) or w.startswith(name[:3]):
                try:
                    return datetime(datetime.now().year, num, d)
                except ValueError:
                    return None
    return None


@bot.on.message()
async def any_text_handler(message: Message):
    if not message.text:
        return
    target = parse_date_text(message.text)
    if not target:
        await message.answer("Не понял дату. Попробуйте: 02.10, 2 октября.",
                             keyboard=get_main_keyboard())
        return
    tables = await asyncio.to_thread(parse_schedule_for_date, target)
    if not tables:
        await message.answer("📭 На эту дату нет расписания.", keyboard=get_main_keyboard())
        return
    await message.answer(format_multiple_tables(tables), keyboard=get_main_keyboard())


async def scheduler_loop():
    while True:
        try:
            await asyncio.to_thread(check_schedule_changes)
        except Exception as e:
            logger.error(f"Ошибка проверки расписания: {e}")
        try:
            await asyncio.to_thread(check_cloud_and_notify)
        except Exception as e:
            logger.error(f"Ошибка проверки облака: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


async def main():
    global loop
    loop = asyncio.get_running_loop()
    load_cache()
    asyncio.create_task(scheduler_loop())
    logger.info(f"ВК-бот запущен. Группа {TARGET_GROUP}. Интервал: {CHECK_INTERVAL} сек.")
    await bot.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
