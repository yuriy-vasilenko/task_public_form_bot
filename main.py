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
from fastapi import FastAPI, Form, Request
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
DAILY_ALL_HOUR_MSK = 16
DAILY_ALL_MINUTE_MSK = 30

# max printable width inside <pre> per chunk (Telegram limit 4096 per message)
TG_TABLE_CHUNK = 3400

app = FastAPI(title=APP_TITLE)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
            priority TEXT NOT NULL DEFAULT 'Обычный',
            status TEXT NOT NULL DEFAULT 'Новая',
            source TEXT NOT NULL DEFAULT 'public_form'
        )
        """
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
                    for chat_id in get_active_subscribers():
                        send_all_tasks_table(chat_id)
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


def send_all_tasks_table(chat_id: str) -> None:
    rows = fetchall("SELECT * FROM tasks ORDER BY id DESC", ())
    if not rows:
        tg_send_message(
            chat_id,
            "<b>Все записи</b>\n\nВ системе пока нет задач.",
            build_reply_keyboard(),
        )
        return

    record_blocks = _all_tasks_record_blocks(rows)
    total = len(rows)

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

    n_batches = len(batches)
    for i, batch_blks in enumerate(batches):
        part = f" (часть {i + 1}/{n_batches})" if n_batches > 1 else ""
        body = "\n\n".join(batch_blks)
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
    today = datetime.now().strftime("%Y-%m-%d")
    return fetchall(
        "SELECT * FROM tasks WHERE substr(created_at,1,10)=? ORDER BY id DESC LIMIT ?",
        (today, limit),
    )



def get_recent_tasks(limit: int = 10) -> list[sqlite3.Row]:
    return fetchall("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))



def get_last_week_tasks(limit: int = 20) -> list[sqlite3.Row]:
    border = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
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
        "• <b>Ежедневно в 16:30 МСК</b> бот автоматически присылает полную таблицу всех записей.\n"
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
            "Также ежедневно в <b>16:30 МСК</b> бот отправляет полную таблицу автоматически."
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
        },
    )


@app.post("/submit", response_class=HTMLResponse)
def submit_task(
    request: Request,
    initiator: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    contact: str = Form(""),
    priority: str = Form("Обычный"),
):
    task_id = execute(
        """
        INSERT INTO tasks (created_at, initiator, title, description, contact, priority, status, source)
        VALUES (?, ?, ?, ?, ?, ?, 'Новая', 'public_form')
        """,
        (
            now_str(),
            initiator.strip(),
            title.strip(),
            description.strip(),
            contact.strip(),
            priority.strip(),
        ),
    )

    task = fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))

    if NOTIFY_ON_NEW and BOT_TOKEN and ADMIN_CHAT_ID and task:
        msg = (
            f"<b>Новая запись #{task['id']}</b>\n\n"
            f"<b>Инициатор:</b> {html.escape(task['initiator'])}\n"
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
    today = fetchone("SELECT COUNT(*) AS c FROM tasks WHERE substr(created_at,1,10)=?", (datetime.now().strftime("%Y-%m-%d"),))["c"]
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
):
    redirect = require_login(request)
    if redirect:
        return redirect

    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[str] = []

    if q.strip():
        query += " AND (initiator LIKE ? OR title LIKE ? OR description LIKE ? OR contact LIKE ?)"
        term = f"%{q.strip()}%"
        params.extend([term, term, term, term])
    if status.strip():
        query += " AND status = ?"
        params.append(status.strip())
    if priority.strip():
        query += " AND priority = ?"
        params.append(priority.strip())

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
        {"page_title": f"Редактирование #{task_id}", "task": task},
    )


@app.post("/admin/task/{task_id}/edit")
def admin_task_edit(
    task_id: int,
    request: Request,
    initiator: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    contact: str = Form(""),
    priority: str = Form("Обычный"),
    status: str = Form("Новая"),
):
    redirect = require_login(request)
    if redirect:
        return redirect

    execute(
        """
        UPDATE tasks
        SET initiator = ?, title = ?, description = ?, contact = ?, priority = ?, status = ?
        WHERE id = ?
        """,
        (
            initiator.strip(),
            title.strip(),
            description.strip(),
            contact.strip(),
            priority.strip(),
            status.strip(),
            task_id,
        ),
    )
    return RedirectResponse(url=f"/admin/task/{task_id}", status_code=303)


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
