import html
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "tasks.db")))
TIMEZONE_LABEL = os.getenv("TIMEZONE_LABEL", "Europe/Vilnius")
APP_TITLE = os.getenv("APP_TITLE", "Форма задач + Telegram бот")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))


def get_admin_credentials() -> tuple[str, str]:
    """Читает логин/пароль из .env при каждом входе (без перезапуска сервера)."""
    load_dotenv(BASE_DIR / ".env", override=True)
    u = os.getenv("ADMIN_USERNAME", "admin").strip()
    p = os.getenv("ADMIN_PASSWORD", "Qwerty00").strip()
    return u, p
BASE_URL = os.getenv("BASE_URL", "")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Опционально: куда слать NOTIFY_ON_NEW; команды бота доступны любому пользователю Telegram
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
NOTIFY_ON_NEW = os.getenv("NOTIFY_ON_NEW", "0") == "1"
# Локально без HTTPS: long polling (снимает webhook — не включайте на проде вместе с webhook)
TELEGRAM_POLLING = os.getenv("TELEGRAM_POLLING", "0").strip().lower() in ("1", "true", "yes")
try:
    MOSCOW_TZ = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    # Windows без tzdata: для Москвы достаточно фиксированного UTC+3.
    from datetime import timezone

    MOSCOW_TZ = timezone(timedelta(hours=3))

try:
    APP_TZ = ZoneInfo(TIMEZONE_LABEL)
except ZoneInfoNotFoundError:
    APP_TZ = MOSCOW_TZ
DAILY_ALL_HOUR_MSK = 9
DAILY_ALL_MINUTE_MSK = 0
TASK_STATUSES = ["Новая", "В работе", "Выполнена", "Отложена"]
TASK_PRIORITIES = ["Обычный", "Высокий", "Срочный", "Низкий"]
RESPONSIBLES = ["Микиртумов", "Березовой"]
DEFAULT_RESPONSIBLE = "Микиртумов"
DEPARTMENTS = [
    "Механик",
    "Стр МУ",
    "Диспетчер",
    "Ассист Г",
    "Ассист Д",
    "Бухгалтерия",
    "Юр отдел",
    "Сист Админ",
    "Энерго уч",
    "Не назначено",
]
DEFAULT_DEPARTMENT = "Не назначено"

# max printable width inside <pre> per chunk (Telegram limit 4096 per message)
TG_TABLE_CHUNK = 3400

app = FastAPI(title=APP_TITLE)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def now_str() -> str:
    return datetime.now(APP_TZ).strftime("%Y-%m-%d %H:%M:%S")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            initiator TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            contact TEXT,
            department TEXT NOT NULL DEFAULT 'Не назначено',
            assignee TEXT NOT NULL DEFAULT '',
            due_at TEXT,
            planned_minutes INTEGER NOT NULL DEFAULT 0,
            priority TEXT NOT NULL DEFAULT 'Обычный',
            status TEXT NOT NULL DEFAULT 'Новая',
            source TEXT NOT NULL DEFAULT 'public_form'
        )
        """
    )
    task_columns = {str(row["name"]) for row in cur.execute("PRAGMA table_info(tasks)").fetchall()}
    if "department" not in task_columns:
        cur.execute("ALTER TABLE tasks ADD COLUMN department TEXT NOT NULL DEFAULT 'Не назначено'")
    if "assignee" not in task_columns:
        cur.execute("ALTER TABLE tasks ADD COLUMN assignee TEXT NOT NULL DEFAULT ''")
    if "due_at" not in task_columns:
        cur.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")
    if "planned_minutes" not in task_columns:
        cur.execute("ALTER TABLE tasks ADD COLUMN planned_minutes INTEGER NOT NULL DEFAULT 0")
    cur.execute(
        """
        UPDATE tasks
        SET department = ?
        WHERE department IS NULL OR trim(department) = ''
        """,
        (DEFAULT_DEPARTMENT,),
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tg_subscribers (
            chat_id TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_reports (
            report_date TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            report_text TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def _telegram_polling_loop() -> None:
    offset = 0
    while True:
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"timeout": 25, "offset": offset, "allowed_updates": ["message", "callback_query"]},
                timeout=30,
            )
            data = response.json()
            if not data.get("ok"):
                time.sleep(2)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                if update.get("message"):
                    process_telegram_message(update["message"])
                elif update.get("callback_query"):
                    process_telegram_callback(update["callback_query"])
        except Exception:
            time.sleep(3)


def _start_telegram_polling() -> None:
    if not TELEGRAM_POLLING or not BOT_TOKEN:
        return
    tg_api("deleteWebhook", {"drop_pending_updates": False})
    thread = threading.Thread(target=_telegram_polling_loop, daemon=True, name="telegram-poll")
    thread.start()


def _daily_all_records_loop() -> None:
    while True:
        try:
            if not BOT_TOKEN:
                time.sleep(20)
                continue
            now_msk = datetime.now(MOSCOW_TZ)
            is_target_time = now_msk.hour == DAILY_ALL_HOUR_MSK and now_msk.minute == DAILY_ALL_MINUTE_MSK
            if is_target_time:
                today = now_msk.strftime("%Y-%m-%d")
                if _claim_daily_all_slot(today):
                    generate_and_send_daily_report(today)
            time.sleep(20)
        except Exception:
            time.sleep(20)


def _start_daily_all_records_scheduler() -> None:
    if not BOT_TOKEN:
        return
    thread = threading.Thread(target=_daily_all_records_loop, daemon=True, name="daily-all-scheduler")
    thread.start()


@app.on_event("startup")
def startup_event() -> None:
    init_db()
    _start_telegram_polling()
    _start_daily_all_records_scheduler()


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("admin_logged_in"))


def require_login(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


def fetchone(query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(query, params)
    row = cur.fetchone()
    conn.close()
    return row


def fetchall(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def execute(query: str, params: tuple = ()) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(query, params)
    conn.commit()
    last_id = cur.lastrowid
    conn.close()
    return last_id


def upsert_subscriber(chat_id: str) -> None:
    if not chat_id:
        return
    conn = get_conn()
    cur = conn.cursor()
    now = now_str()
    cur.execute(
        """
        INSERT INTO tg_subscribers (chat_id, first_seen_at, last_seen_at, is_active)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(chat_id) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            is_active = 1
        """,
        (chat_id, now, now),
    )
    conn.commit()
    conn.close()


def get_active_subscribers() -> list[str]:
    rows = fetchall("SELECT chat_id FROM tg_subscribers WHERE is_active = 1 ORDER BY chat_id ASC")
    return [str(r["chat_id"]) for r in rows]


def _claim_daily_all_slot(today: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('daily_all_sent_date', '')")
    cur.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'daily_all_sent_date' AND value <> ?",
        (today, today),
    )
    changed = cur.rowcount == 1
    conn.commit()
    conn.close()
    return changed


def save_daily_report(report_date: str, report_text: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO daily_reports (report_date, generated_at, report_text)
        VALUES (?, ?, ?)
        ON CONFLICT(report_date) DO UPDATE SET
            generated_at = excluded.generated_at,
            report_text = excluded.report_text
        """,
        (report_date, now_str(), report_text),
    )
    conn.commit()
    conn.close()


def get_daily_reports(limit: int = 30) -> list[sqlite3.Row]:
    return fetchall(
        "SELECT report_date, generated_at, report_text FROM daily_reports ORDER BY report_date DESC LIMIT ?",
        (limit,),
    )


def generate_and_send_daily_report(report_date: str) -> None:
    rows = fetchall("SELECT * FROM tasks ORDER BY id DESC", ())
    save_daily_report(report_date, build_daily_report_text(rows))
    for chat_id in get_active_subscribers():
        send_all_tasks_table(chat_id, rows)


def tg_api(method: str, payload: dict) -> dict:
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN not configured"}
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=payload,
            timeout=20,
        )
        return response.json()
    except Exception as exc:
        return {"ok": False, "description": str(exc)}



def tg_send_message(chat_id: str, text: str, reply_markup: Optional[dict] = None) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", payload)



def tg_answer_callback(callback_query_id: str, text: str = "") -> None:
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    tg_api("answerCallbackQuery", payload)



def build_reply_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "Все записи"}, {"text": "Сводка за сегодня"}],
            [{"text": "Последние 10"}, {"text": "Новые за 7 дней"}],
            [{"text": "Помощь"}],
        ],
        "resize_keyboard": True,
    }



def short(text: str, limit: int = 28) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."



def pad(text: str, width: int) -> str:
    raw = short(text, width)
    return raw[:width].ljust(width)



def _one_line_cell(value: object, max_len: int) -> str:
    s = str(value or "").replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _norm_multiline(value: object) -> str:
    return str(value or "").replace("\n", " ").strip()


def _record_blocks_for_row(row: sqlite3.Row) -> list[str]:
    sep = "-" * 36
    dt = _norm_multiline(row["created_at"])
    ini = _norm_multiline(row["initiator"])
    tit = _norm_multiline(row["title"])
    head = f"{sep}\n{dt}\nИнициатор: {ini}\nКратко:"
    budget = max(400, TG_TABLE_CHUNK - len(head) - 24)
    if len(tit) <= budget:
        return [f"{head} {tit}"]
    parts: list[str] = []
    for i in range(0, len(tit), budget):
        chunk = tit[i : i + budget]
        if i == 0:
            parts.append(f"{head} {chunk}")
        else:
            parts.append(f"{sep}\n(продолжение той же задачи)\n{chunk}")
    return parts


def _all_tasks_record_blocks(rows: list[sqlite3.Row]) -> list[str]:
    blocks: list[str] = []
    for row in rows:
        blocks.extend(_record_blocks_for_row(row))
    return blocks


def _all_tasks_bodies(rows: list[sqlite3.Row]) -> list[str]:
    record_blocks = _all_tasks_record_blocks(rows)
    batches: list[list[str]] = []
    batch: list[str] = []
    batch_len = 0
    for blk in record_blocks:
        add = len(blk) + 2
        if batch and batch_len + add > TG_TABLE_CHUNK:
            batches.append(batch)
            batch = []
            batch_len = 0
        batch.append(blk)
        batch_len += add
    if batch:
        batches.append(batch)
    return ["\n\n".join(b) for b in batches]


def build_daily_report_text(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "В системе пока нет задач."
    bodies = _all_tasks_bodies(rows)
    n_batches = len(bodies)
    chunks: list[str] = []
    for i, body in enumerate(bodies):
        part = f" (часть {i + 1}/{n_batches})" if n_batches > 1 else ""
        chunks.append(f"Все записи{part}\n\n{body}")
    chunks.append(f"Всего записей: {len(rows)}")
    return "\n\n".join(chunks)


def send_all_tasks_table(chat_id: str, rows: Optional[list[sqlite3.Row]] = None) -> None:
    if rows is None:
        rows = fetchall("SELECT * FROM tasks ORDER BY id DESC", ())
    if not rows:
        tg_send_message(
            chat_id,
            "<b>Все записи</b>\n\nВ системе пока нет задач.",
            build_reply_keyboard(),
        )
        return

    total = len(rows)
    bodies = _all_tasks_bodies(rows)
    n_batches = len(bodies)
    for i, body in enumerate(bodies):
        part = f" (часть {i + 1}/{n_batches})" if n_batches > 1 else ""
        footer = ""
        if i == n_batches - 1:
            footer = f"\n\n<i>Всего записей: {total}</i>"
        text = f"<b>Все записи</b>{part}\n\n<pre>{html.escape(body)}</pre>" + footer
        tg_send_message(chat_id, text, build_reply_keyboard() if i == n_batches - 1 else None)


def summary_table(rows: list[sqlite3.Row], title: str) -> str:
    if not rows:
        return f"<b>{html.escape(title)}</b>\n\nНовых записей нет."

    header = f"<b>{html.escape(title)}</b>\n\n"
    lines = ["№  Время  Инициатор      Задача", "-- ------ -------------- ----------------------------"]

    for row in rows:
        created = row["created_at"]
        try:
            dt = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
            time_part = dt.strftime("%H:%M")
        except Exception:
            time_part = created[-8:-3] if len(created) >= 16 else created
        line = f"{str(row['id']).rjust(2)} {time_part}  {pad(row['initiator'], 14)} {pad(row['title'], 28)}"
        lines.append(line)

    body = "\n".join(lines)
    footer = f"\n\nВсего: {len(rows)}"
    return header + f"<pre>{html.escape(body)}</pre>" + footer



def detail_text(task: sqlite3.Row) -> str:
    return (
        f"<b>Задача #{task['id']}</b>\n\n"
        f"<b>Дата:</b> {html.escape(task['created_at'])}\n"
        f"<b>Инициатор:</b> {html.escape(task['initiator'])}\n"
        f"<b>Отдел:</b> {html.escape(task['department'] or DEFAULT_DEPARTMENT)}\n"
        f"<b>Кратко:</b> {html.escape(task['title'])}\n"
        f"<b>Описание:</b> {html.escape(task['description'] or '—')}\n"
        f"<b>Контакт:</b> {html.escape(task['contact'] or '—')}\n"
        f"<b>Приоритет:</b> {html.escape(task['priority'])}\n"
        f"<b>Статус:</b> {html.escape(task['status'])}\n"
        f"<b>Источник:</b> {html.escape(task['source'])}"
    )



def detail_keyboard(task_id: int) -> dict:
    buttons = [
        [{"text": "Все записи", "callback_data": "list:all"}],
        [{"text": "Сводка за сегодня", "callback_data": "list:today"}, {"text": "Последние 10", "callback_data": "list:recent"}],
        [{"text": "Новые за 7 дней", "callback_data": "list:week"}],
    ]
    if BASE_URL:
        buttons.append([{"text": "Открыть админку", "url": f"{BASE_URL.rstrip('/')}/admin"}])
    return {"inline_keyboard": buttons}



def summary_keyboard(rows: list[sqlite3.Row], mode: str) -> dict:
    inline_keyboard = []
    for row in rows[:10]:
        inline_keyboard.append([
            {"text": f"Подробнее #{row['id']}", "callback_data": f"detail:{row['id']}"}
        ])
    inline_keyboard.append([
        {"text": "Сегодня", "callback_data": "list:today"},
        {"text": "Последние 10", "callback_data": "list:recent"},
    ])
    inline_keyboard.append([
        {"text": "Все записи", "callback_data": "list:all"},
        {"text": "Новые за 7 дней", "callback_data": "list:week"},
    ])
    inline_keyboard.append([{"text": "Помощь", "callback_data": "help"}])
    return {"inline_keyboard": inline_keyboard}



def get_today_tasks(limit: int = 10) -> list[sqlite3.Row]:
    today = datetime.now(APP_TZ).strftime("%Y-%m-%d")
    return fetchall(
        "SELECT * FROM tasks WHERE substr(created_at,1,10)=? ORDER BY id DESC LIMIT ?",
        (today, limit),
    )



def get_recent_tasks(limit: int = 10) -> list[sqlite3.Row]:
    return fetchall("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))



def get_last_week_tasks(limit: int = 20) -> list[sqlite3.Row]:
    border = (datetime.now(APP_TZ) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    return fetchall("SELECT * FROM tasks WHERE created_at >= ? ORDER BY id DESC LIMIT ?", (border, limit))



def send_summary(chat_id: str, mode: str = "today") -> None:
    if mode == "recent":
        rows = get_recent_tasks(10)
        title = "Последние 10 записей"
    elif mode == "week":
        rows = get_last_week_tasks(20)
        title = "Новые записи за 7 дней"
    else:
        rows = get_today_tasks(10)
        title = "Сводка за сегодня"

    tg_send_message(chat_id, summary_table(rows, title), summary_keyboard(rows, mode))



def send_help(chat_id: str) -> None:
    text = (
        "<b>Команды бота</b>\n\n"
        "• <b>Все записи</b> — полная таблица по всем задачам в системе "
        "(дата и время, инициатор, краткая формулировка). Доступна по кнопке.\n"
        "• <b>Ежедневно в 09:00 МСК</b> бот автоматически присылает полную таблицу всех записей.\n"
        "• <b>Сводка за сегодня</b> — краткая таблица за текущий день.\n"
        "• <b>Последние 10</b> — последние добавленные записи.\n"
        "• <b>Новые за 7 дней</b> — выборка за неделю.\n"
        "• Под сводками доступны кнопки <b>Подробнее</b> по каждой записи."
    )
    if BASE_URL:
        text += f"\n\n<b>Админка:</b> <a href=\"{html.escape(BASE_URL.rstrip('/') + '/admin')}\">открыть</a>"
    tg_send_message(chat_id, text, {"remove_keyboard": False, **build_reply_keyboard()})



def process_telegram_message(message: dict) -> None:
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    if not chat_id:
        return
    upsert_subscriber(chat_id)

    if text in ["/start", "/help", "Помощь"]:
        welcome = (
            "<b>Бот подключен.</b>\n\n"
            "Кнопка <b>Все записи</b> присылает таблицу по всем задачам в системе. "
            "Также ежедневно в <b>09:00 МСК</b> бот отправляет полную таблицу автоматически."
        )
        tg_send_message(chat_id, welcome, build_reply_keyboard())
        send_help(chat_id)
    elif text in ["/all", "Все записи"]:
        send_all_tasks_table(chat_id)
    elif text in ["/summary", "Сводка за сегодня"]:
        send_summary(chat_id, "today")
    elif text in ["/recent", "Последние 10"]:
        send_summary(chat_id, "recent")
    elif text in ["/week", "Новые за 7 дней"]:
        send_summary(chat_id, "week")
    else:
        tg_send_message(chat_id, "Не понял команду. Нажмите одну из кнопок ниже.", build_reply_keyboard())



def process_telegram_callback(callback_query: dict) -> None:
    data = callback_query.get("data", "")
    callback_id = callback_query.get("id", "")
    message = callback_query.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id:
        upsert_subscriber(chat_id)

    if data.startswith("detail:"):
        task_id = data.split(":", 1)[1]
        task = fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not task:
            tg_answer_callback(callback_id, "Запись не найдена")
            return
        tg_answer_callback(callback_id)
        tg_send_message(chat_id, detail_text(task), detail_keyboard(int(task_id)))
    elif data == "list:all":
        tg_answer_callback(callback_id)
        send_all_tasks_table(chat_id)
    elif data == "list:today":
        tg_answer_callback(callback_id)
        send_summary(chat_id, "today")
    elif data == "list:recent":
        tg_answer_callback(callback_id)
        send_summary(chat_id, "recent")
    elif data == "list:week":
        tg_answer_callback(callback_id)
        send_summary(chat_id, "week")
    elif data == "help":
        tg_answer_callback(callback_id)
        send_help(chat_id)
    else:
        tg_answer_callback(callback_id, "Неизвестная команда")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "app": APP_TITLE, "time": now_str()}


@app.get("/", response_class=HTMLResponse)
def public_form(request: Request):
    return templates.TemplateResponse(
        request,
        "public_form.html",
        {
            "page_title": "Отправить задачу",
            "app_title": APP_TITLE,
            "departments": DEPARTMENTS,
            "priorities": TASK_PRIORITIES,
            "statuses": TASK_STATUSES,
            "responsibles": RESPONSIBLES,
            "default_responsible": DEFAULT_RESPONSIBLE,
        },
    )


@app.post("/submit", response_class=HTMLResponse)
def submit_task(
    request: Request,
    initiator: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    contact: str = Form(""),
    department: str = Form(DEFAULT_DEPARTMENT),
    assignee: str = Form(""),
    due_at: str = Form(""),
    planned_minutes: str = Form("0"),
    priority: str = Form("Обычный"),
    status: str = Form("Новая"),
):
    clean_priority = priority.strip() if priority.strip() in TASK_PRIORITIES else "Обычный"
    clean_status = status.strip() if status.strip() in TASK_STATUSES else "Новая"
    clean_assignee = normalize_responsible(assignee)
    task_id = execute(
        """
        INSERT INTO tasks (
            created_at, initiator, title, description, contact, department, assignee, due_at,
            planned_minutes, priority, status, source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'public_form')
        """,
        (
            now_str(),
            initiator.strip(),
            title.strip(),
            description.strip(),
            contact.strip(),
            department.strip() if department.strip() in DEPARTMENTS else DEFAULT_DEPARTMENT,
            clean_assignee,
            normalize_due_at(due_at),
            parse_planned_minutes(planned_minutes),
            clean_priority,
            clean_status,
        ),
    )

    task = fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))

    if NOTIFY_ON_NEW and BOT_TOKEN and ADMIN_CHAT_ID and task:
        msg = (
            f"<b>Новая запись #{task['id']}</b>\n\n"
            f"<b>Инициатор:</b> {html.escape(task['initiator'])}\n"
            f"<b>Отдел:</b> {html.escape(task['department'] or DEFAULT_DEPARTMENT)}\n"
            f"<b>Кратко:</b> {html.escape(task['title'])}\n"
            f"<b>Приоритет:</b> {html.escape(task['priority'])}"
        )
        tg_send_message(
            str(ADMIN_CHAT_ID),
            msg,
            {"inline_keyboard": [[{"text": f"Подробнее #{task['id']}", "callback_data": f"detail:{task['id']}"}]]},
        )

    return templates.TemplateResponse(
        request,
        "submitted.html",
        {
            "page_title": "Заявка отправлена",
            "task": task,
        },
    )


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {
            "page_title": "Вход в админку",
            "error": error,
        },
    )


@app.post("/admin/login")
async def admin_login(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", "")).strip()
    admin_username = str(form.get("admin_username", "")).strip()
    admin_password = str(form.get("admin_password", "")).strip()
    admin_u, admin_p = get_admin_credentials()
    entered_u = admin_username or username
    entered_p = admin_password or password
    u_ok = entered_u == admin_u
    p_ok = entered_p == admin_p
    if u_ok and p_ok:
        request.session["admin_logged_in"] = True
        return RedirectResponse(url="/admin", status_code=303)
    # 200 вместо 401: форма входа не «HTTP API», красная 401 в консоли браузера только путает
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {
            "page_title": "Вход в админку",
            "error": "Неверный логин или пароль",
        },
    )


@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect

    total = fetchone("SELECT COUNT(*) AS c FROM tasks")["c"]
    today = fetchone(
        "SELECT COUNT(*) AS c FROM tasks WHERE substr(created_at,1,10)=?",
        (datetime.now(APP_TZ).strftime("%Y-%m-%d"),),
    )["c"]
    in_work = fetchone("SELECT COUNT(*) AS c FROM tasks WHERE status='В работе'")["c"]
    completed = fetchone("SELECT COUNT(*) AS c FROM tasks WHERE status='Выполнена'")["c"]
    latest = fetchall("SELECT * FROM tasks ORDER BY id DESC LIMIT 8")

    return templates.TemplateResponse(
        request,
        "admin_dashboard.html",
        {
            "page_title": "Админка",
            "total": total,
            "today": today,
            "in_work": in_work,
            "completed": completed,
            "latest": latest,
            "base_url": BASE_URL,
            "bot_ready": bool(BOT_TOKEN),
        },
    )


@app.get("/admin/tasks", response_class=HTMLResponse)
def admin_tasks(
    request: Request,
    q: str = "",
    status: str = "",
    priority: str = "",
    department: str = "",
):
    redirect = require_login(request)
    if redirect:
        return redirect

    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[str] = []

    if q.strip():
        query += " AND (initiator LIKE ? OR title LIKE ? OR description LIKE ? OR contact LIKE ? OR department LIKE ?)"
        term = f"%{q.strip()}%"
        params.extend([term, term, term, term, term])
    if status.strip():
        query += " AND status = ?"
        params.append(status.strip())
    if priority.strip():
        query += " AND priority = ?"
        params.append(priority.strip())
    if department.strip():
        query += " AND department = ?"
        params.append(department.strip())

    query += " ORDER BY id DESC"
    rows = fetchall(query, tuple(params))

    return templates.TemplateResponse(
        request,
        "admin_tasks.html",
        {
            "page_title": "Все задачи",
            "tasks": rows,
            "q": q,
            "status_filter": status,
            "priority_filter": priority,
            "department_filter": department,
            "departments": DEPARTMENTS,
        },
    )


@app.get("/admin/task/{task_id}", response_class=HTMLResponse)
def admin_task_detail(task_id: int, request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect

    task = fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return templates.TemplateResponse(
        request,
        "admin_task_detail.html",
        {"page_title": f"Задача #{task_id}", "task": task},
    )


@app.get("/admin/task/{task_id}/edit", response_class=HTMLResponse)
def admin_task_edit_page(task_id: int, request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect
    task = fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return templates.TemplateResponse(
        request,
        "admin_task_edit.html",
        {
            "page_title": f"Редактирование #{task_id}",
            "task": task,
            "departments": DEPARTMENTS,
            "responsibles": RESPONSIBLES,
            "due_at_input": due_at_for_input(task["due_at"] if task else ""),
        },
    )


@app.post("/admin/task/{task_id}/edit")
def admin_task_edit(
    task_id: int,
    request: Request,
    initiator: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    contact: str = Form(""),
    department: str = Form(DEFAULT_DEPARTMENT),
    assignee: str = Form(""),
    due_at: str = Form(""),
    planned_minutes: str = Form("0"),
    priority: str = Form("Обычный"),
    status: str = Form("Новая"),
):
    redirect = require_login(request)
    if redirect:
        return redirect

    execute(
        """
        UPDATE tasks
        SET initiator = ?, title = ?, description = ?, contact = ?, department = ?, assignee = ?, due_at = ?, planned_minutes = ?, priority = ?, status = ?
        WHERE id = ?
        """,
        (
            initiator.strip(),
            title.strip(),
            description.strip(),
            contact.strip(),
            department.strip() if department.strip() in DEPARTMENTS else DEFAULT_DEPARTMENT,
            normalize_responsible(assignee),
            normalize_due_at(due_at),
            parse_planned_minutes(planned_minutes),
            priority.strip() if priority.strip() in TASK_PRIORITIES else "Обычный",
            status.strip() if status.strip() in TASK_STATUSES else "Новая",
            task_id,
        ),
    )
    return RedirectResponse(url=f"/admin/task/{task_id}", status_code=303)


@app.get("/admin/board", response_class=HTMLResponse)
def admin_board(request: Request, responsible: str = DEFAULT_RESPONSIBLE):
    redirect = require_login(request)
    if redirect:
        return redirect
    active_responsible = normalize_responsible(responsible)

    return templates.TemplateResponse(
        request,
        "admin_board.html",
        {
            "page_title": "Доска распределения",
            "board_columns": build_board_columns(active_responsible),
            "responsibles": RESPONSIBLES,
            "active_responsible": active_responsible,
        },
    )


@app.get("/admin/board/department/{dep_idx}", response_class=HTMLResponse)
def admin_board_department(request: Request, dep_idx: int):
    redirect = require_login(request)
    if redirect:
        return redirect

    dep_name = get_department_by_idx(dep_idx)
    return templates.TemplateResponse(
        request,
        "admin_board_department.html",
        {
            "page_title": f"Отдел: {dep_name}",
            "department": dep_name,
            "tasks": get_department_tasks(dep_name),
        },
    )


def get_department_by_idx(dep_idx: int) -> str:
    if dep_idx < 0 or dep_idx >= len(DEPARTMENTS):
        raise HTTPException(status_code=404, detail="department not found")
    return DEPARTMENTS[dep_idx]


def get_department_tasks(dep_name: str) -> list[sqlite3.Row]:
    return fetchall("SELECT * FROM tasks WHERE department = ? ORDER BY id DESC", (dep_name,))


def parse_dt(value: str) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("T", " ")
    if len(normalized) == 16:
        normalized += ":00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


def normalize_due_at(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    normalized = raw.replace("T", " ")
    if len(normalized) == 16:
        normalized += ":00"
    return normalized


def due_at_for_input(value: str) -> str:
    dt = parse_dt(value)
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M")


def parse_planned_minutes(raw_value: str) -> int:
    raw = (raw_value or "").strip()
    if not raw:
        return 0
    try:
        minutes = int(float(raw))
    except ValueError:
        return 0
    return max(0, min(minutes, 60 * 24 * 30))


def normalize_responsible(value: str) -> str:
    value = (value or "").strip()
    key_map = {name.lower().replace("ё", "е"): name for name in RESPONSIBLES}
    key = value.lower().replace("ё", "е")
    return key_map.get(key, DEFAULT_RESPONSIBLE)


def build_board_columns(responsible: str = "") -> list[dict]:
    if responsible:
        rows = fetchall("SELECT * FROM tasks WHERE assignee = ? ORDER BY id DESC", (normalize_responsible(responsible),))
    else:
        rows = fetchall("SELECT * FROM tasks ORDER BY id DESC", ())
    columns = {dep: {"department": dep, "count": 0, "tasks": []} for dep in DEPARTMENTS}
    now_local = datetime.now(APP_TZ).replace(tzinfo=None)

    for row in rows:
        dep = (row["department"] or DEFAULT_DEPARTMENT).strip()
        if dep not in columns:
            dep = DEFAULT_DEPARTMENT

        due_value = (row["due_at"] or "").strip()
        due_dt = parse_dt(due_value)
        status = (row["status"] or "").strip()
        is_overdue = bool(due_dt and due_dt < now_local and status != "Выполнена")

        columns[dep]["count"] += 1
        columns[dep]["tasks"].append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "title": row["title"],
                "initiator": row["initiator"],
                "assignee": (row["assignee"] or "").strip(),
                "planned_minutes": int(row["planned_minutes"] or 0),
                "priority": row["priority"],
                "status": status,
                "due_at": due_value,
                "is_overdue": is_overdue,
            }
        )

    return [columns[dep] for dep in DEPARTMENTS]


def get_department_counts() -> dict[str, int]:
    counts = {dep: 0 for dep in DEPARTMENTS}
    rows = fetchall("SELECT department, COUNT(*) AS c FROM tasks GROUP BY department", ())
    for row in rows:
        dep = (row["department"] or DEFAULT_DEPARTMENT).strip()
        if dep not in counts:
            dep = DEFAULT_DEPARTMENT
        counts[dep] += int(row["c"])
    return counts


@app.get("/board", response_class=HTMLResponse)
def public_board(request: Request, responsible: str = DEFAULT_RESPONSIBLE):
    active_responsible = normalize_responsible(responsible)
    return templates.TemplateResponse(
        request,
        "public_board.html",
        {
            "page_title": "Доска задач по отделам",
            "board_columns": build_board_columns(active_responsible),
            "responsibles": RESPONSIBLES,
            "active_responsible": active_responsible,
        },
    )


@app.get("/board/department/{dep_idx}", response_class=HTMLResponse)
def public_board_department(request: Request, dep_idx: int):
    dep_name = get_department_by_idx(dep_idx)
    return templates.TemplateResponse(
        request,
        "public_board_department.html",
        {
            "page_title": f"Отдел: {dep_name}",
            "department": dep_name,
            "tasks": get_department_tasks(dep_name),
        },
    )


@app.post("/admin/task/{task_id}/delete")
def admin_task_delete(task_id: int, request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect

    execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return RedirectResponse(url="/admin/tasks", status_code=303)


@app.get("/admin/summary", response_class=HTMLResponse)
def admin_summary(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect

    today_tasks = get_today_tasks(100)
    recent_tasks = get_recent_tasks(20)
    return templates.TemplateResponse(
        request,
        "admin_summary.html",
        {
            "page_title": "Краткая сводка",
            "today_tasks": today_tasks,
            "recent_tasks": recent_tasks,
        },
    )


@app.get("/admin/reports", response_class=HTMLResponse)
def admin_reports(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect

    reports = get_daily_reports(60)
    return templates.TemplateResponse(
        request,
        "admin_reports.html",
        {
            "page_title": "Ежедневные отчеты 09:00",
            "reports": reports,
            "generated_now": request.query_params.get("generated") == "1",
        },
    )


@app.post("/admin/reports/generate")
def admin_reports_generate_now(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect

    report_date = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d")
    generate_and_send_daily_report(report_date)
    return RedirectResponse(url="/admin/reports?generated=1", status_code=303)


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if TELEGRAM_WEBHOOK_SECRET:
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header_secret != TELEGRAM_WEBHOOK_SECRET:
            return JSONResponse({"ok": False, "error": "invalid secret"}, status_code=403)

    update = await request.json()

    if update.get("message"):
        process_telegram_message(update["message"])
    elif update.get("callback_query"):
        process_telegram_callback(update["callback_query"])

    return {"ok": True}
