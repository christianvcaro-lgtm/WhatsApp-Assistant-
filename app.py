import os
import json
import sqlite3
import httpx
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response, Query
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from openai import OpenAI

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "653078644555574")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "mi_asistente_personal_2024")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MY_PHONE_NUMBER = os.environ.get("MY_PHONE_NUMBER", "")
TIMEZONE = os.environ.get("TIMEZONE", "America/Bogota")
DB_PATH = os.environ.get("DB_PATH", "assistant.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
tz = ZoneInfo(TIMEZONE)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "title TEXT NOT NULL,"
            "description TEXT,"
            "priority TEXT DEFAULT 'media',"
            "category TEXT DEFAULT 'general',"
            "due_date TEXT,"
            "status TEXT DEFAULT 'pendiente',"
            "created_at TEXT DEFAULT (datetime('now')),"
            "completed_at TEXT);"
            "CREATE TABLE IF NOT EXISTS ideas ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "content TEXT NOT NULL,"
            "category TEXT DEFAULT 'general',"
            "tags TEXT DEFAULT '[]',"
            "created_at TEXT DEFAULT (datetime('now')),"
            "reviewed INTEGER DEFAULT 0);"
            "CREATE TABLE IF NOT EXISTS reminders ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "message TEXT NOT NULL,"
            "remind_at TEXT NOT NULL,"
            "sent INTEGER DEFAULT 0,"
            "created_at TEXT DEFAULT (datetime('now')));"
        )


openai_client = OpenAI(api_key=OPENAI_API_KEY)


def build_system_prompt():
    now = datetime.now(tz)
    current_date = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")
    prompt = (
        "Eres el asistente personal de Christian por WhatsApp. "
        "Maneja YAVE (CRM WhatsApp para inmobiliarias) y Los Lagos (lotes campestres Cartagena). "
        "Te habla en español colombiano informal.\n\n"
        "RESPONDE SOLO JSON VALIDO. Sin markdown, sin backticks.\n\n"
        "Estructura:\n"
        '{"intent": "task|idea|reminder|query|complete|chat", '
        '"data": {...}, '
        '"response": "Respuesta corta para WhatsApp"}\n\n'
        "INTENTS:\n"
        '- "tengo que","necesito","hay que","pendiente","hacer","tarea" = task\n'
        '- "idea","se me ocurrio","que tal si","podriamos" = idea\n'
        '- "recuerdame","no se me olvide","avisame","a las X" = reminder\n'
        '- "que tengo","pendientes","resumen","como voy","mis tareas","mis ideas" = query\n'
        '- "listo","hecho","ya hice","termine","complete" = complete\n'
        "- Otra cosa = chat (responde y sugiere si quiere guardarlo)\n\n"
        "DATA POR INTENT:\n"
        'task: {"title":"corto","description":"detalle o null","priority":"alta|media|baja",'
        '"category":"yave|loslagos|personal|general","due_date":"YYYY-MM-DD o null"}\n'
        'idea: {"content":"la idea","category":"yave|loslagos|personal|general","tags":["tag1"]}\n'
        'reminder: {"message":"que recordar","remind_at":"YYYY-MM-DD HH:MM"}\n'
        'query: {"query_type":"pending_tasks|ideas|today|overdue|category",'
        '"category":"yave|loslagos|personal|general o null"}\n'
        'complete: {"search_term":"texto para buscar la tarea"}\n'
        "chat: {}\n\n"
        "PRIORIDAD: urgente/hoy/asap=alta | fecha<3dias=alta | sin fecha=media | cuando pueda=baja\n"
        "CATEGORIA: WhatsApp,CRM,API,Meta,codigo=yave | lotes,Cartagena,ventas=loslagos | "
        "gym,padel,familia=personal | resto=general\n\n"
        "FECHA: " + current_date + " | HORA: " + current_time + "\n\n"
        "Para reminders: calcula fecha/hora real. "
        '"manana a las 8" = fecha de manana 08:00. '
        '"en 2 horas" = suma desde hora actual.'
    )
    return prompt


async def interpret_message(text):
    prompt = build_system_prompt()
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=1024,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text}
            ]
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("JSON parse error. Raw: %s", raw)
        return {"intent": "chat", "data": {}, "response": "No entendi bien, puedes reformularlo?"}
    except Exception as e:
        logger.error("OpenAI error: %s", e)
        return {"intent": "chat", "data": {}, "response": "Error procesando. Intenta de nuevo."}


def add_task(data):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title,description,priority,category,due_date) VALUES (?,?,?,?,?)",
            (data.get("title", "Sin titulo"), data.get("description"),
             data.get("priority", "media"), data.get("category", "general"), data.get("due_date"))
        )
        return cur.lastrowid


def add_idea(data):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (content,category,tags) VALUES (?,?,?)",
            (data.get("content", ""), data.get("category", "general"), json.dumps(data.get("tags", [])))
        )
        return cur.lastrowid


def add_reminder(data):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (message,remind_at) VALUES (?,?)",
            (data.get("message", ""), data.get("remind_at", ""))
        )
        return cur.lastrowid


def complete_task(search_term):
    with get_db() as conn:
        tasks = conn.execute(
            "SELECT id,title FROM tasks WHERE status='pendiente' AND LOWER(title) LIKE ?",
            ("%" + search_term.lower() + "%",)
        ).fetchall()
        if len(tasks) == 1:
            conn.execute(
                "UPDATE tasks SET status='completada',completed_at=datetime('now') WHERE id=?",
                (tasks[0]["id"],)
            )
            return tasks[0]["title"]
        elif len(tasks) > 1:
            return "MULTIPLE:" + ", ".join(t["title"] for t in tasks)
        return None


def get_pending_tasks(category=None):
    with get_db() as conn:
        if category and category != "null":
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status='pendiente' AND category=? "
                "ORDER BY CASE priority WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END, due_date",
                (category,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status='pendiente' "
                "ORDER BY CASE priority WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END, due_date"
            ).fetchall()
        return [dict(r) for r in rows]


def get_overdue_tasks():
    today = datetime.now(tz).strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status='pendiente' AND due_date<? AND due_date IS NOT NULL",
            (today,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_ideas(limit=10):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM ideas ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_today_summary():
    today = datetime.now(tz).strftime("%Y-%m-%d")
    with get_db() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) as c FROM tasks WHERE status='pendiente'"
        ).fetchone()["c"]
        high = conn.execute(
            "SELECT COUNT(*) as c FROM tasks WHERE status='pendiente' AND priority='alta'"
        ).fetchone()["c"]
        due_today = conn.execute(
            "SELECT * FROM tasks WHERE status='pendiente' AND due_date=?", (today,)
        ).fetchall()
        overdue = conn.execute(
            "SELECT * FROM tasks WHERE status='pendiente' AND due_date<? AND due_date IS NOT NULL",
            (today,)
        ).fetchall()
        completed = conn.execute(
            "SELECT COUNT(*) as c FROM tasks WHERE completed_at LIKE ?", (today + "%",)
        ).fetchone()["c"]
        ideas = conn.execute(
            "SELECT COUNT(*) as c FROM ideas WHERE created_at LIKE ?", (today + "%",)
        ).fetchone()["c"]
        return {
            "pending": pending, "high_priority": high,
            "due_today": [dict(r) for r in due_today],
            "overdue": [dict(r) for r in overdue],
            "completed_today": completed, "ideas_today": ideas
        }


def get_pending_reminders():
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE sent=0 AND remind_at<=?", (now,)
        ).fetchall()
        return [dict(r) for r in rows]


def mark_reminder_sent(rid):
    with get_db() as conn:
        conn.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))


P_EMOJI = {"alta": "\U0001f534", "media": "\U0001f7e1", "baja": "\U0001f7e2"}
C_EMOJI = {"yave": "\U0001f916", "loslagos": "\U0001f3e1", "personal": "\U0001f464", "general": "\U0001f4cc"}


def format_tasks(tasks):
    if not tasks:
        return "\u2705 Sin tareas pendientes."
    lines = ["\U0001f4cb *PENDIENTES*\n"]
    for t in tasks:
        p = P_EMOJI.get(t["priority"], "\u26aa")
        c = C_EMOJI.get(t["category"], "\U0001f4cc")
        due = " \u23f0" + t["due_date"] if t.get("due_date") else ""
        lines.append(p + c + " " + t["title"] + due)
    return "\n".join(lines)


def format_ideas(ideas):
    if not ideas:
        return "\U0001f4a1 No hay ideas guardadas."
    lines = ["\U0001f4a1 *IDEAS RECIENTES*\n"]
    for i in ideas:
        c = C_EMOJI.get(i["category"], "\U0001f4cc")
        lines.append(c + " " + i["content"])
    return "\n".join(lines)


def format_summary(s):
    lines = [
        "\U0001f4ca *RESUMEN DEL DIA*\n",
        "\U0001f4cb Pendientes: *" + str(s["pending"]) + "* (" + str(s["high_priority"]) + " urgentes)",
        "\u2705 Completadas hoy: *" + str(s["completed_today"]) + "*",
        "\U0001f4a1 Ideas hoy: *" + str(s["ideas_today"]) + "*",
    ]
    if s["overdue"]:
        lines.append("\n\u26a0\ufe0f *VENCIDAS (" + str(len(s["overdue"])) + "):*")
        for t in s["overdue"]:
            lines.append("  \U0001f534 " + t["title"] + " (vencia " + t["due_date"] + ")")
    if s["due_today"]:
        lines.append("\n\U0001f4c5 *PARA HOY (" + str(len(s["due_today"])) + "):*")
        for t in s["due_today"]:
            lines.append("  \u27a1\ufe0f " + t["title"])
    return "\n".join(lines)


async def send_whatsapp(to, message):
    url = "https://graph.facebook.com/v22.0/" + WHATSAPP_PHONE_ID + "/messages"
    headers = {"Authorization": "Bearer " + WHATSAPP_TOKEN, "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": message}}
    async with httpx.AsyncClient() as http:
        try:
            resp = await http.post(url, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("Mensaje enviado a %s", to)
        except Exception as e:
            logger.error("Error enviando WhatsApp: %s", e)


async def process_message(phone, text):
    lower = text.strip().lower()
    if lower in ["resumen", "como voy", "status"]:
        return await send_whatsapp(phone, format_summary(get_today_summary()))
    if lower in ["pendientes", "tareas", "mis tareas"]:
        return await send_whatsapp(phone, format_tasks(get_pending_tasks()))
    if lower in ["ideas", "mis ideas"]:
        return await send_whatsapp(phone, format_ideas(get_recent_ideas()))
    if lower == "ayuda":
        return await send_whatsapp(
            phone,
            "\U0001f916 *COMANDOS RAPIDOS*\n\n"
            "\U0001f4cb *pendientes* = ver tareas\n"
            "\U0001f4a1 *ideas* = ver ideas\n"
            "\U0001f4ca *resumen* = resumen del dia\n\n"
            "O simplemente escribeme natural:\n"
            "- _Tengo que llamar a Juan manana_\n"
            "- _Idea: hacer webinar de YAVE_\n"
            "- _Recuerdame a las 3pm revisar metricas_\n"
            "- _Ya hice lo de Juan_"
        )

    result = await interpret_message(text)
    intent = result.get("intent", "chat")
    data = result.get("data", {})
    response_text = result.get("response", "\U0001f44d")

    if intent == "task":
        tid = add_task(data)
        p = P_EMOJI.get(data.get("priority", "media"), "\U0001f7e1")
        c = C_EMOJI.get(data.get("category", "general"), "\U0001f4cc")
        msg = "\u2705 Tarea #" + str(tid) + " guardada\n" + p + c + " *" + data.get("title", "") + "*"
        if data.get("due_date"):
            msg = msg + "\n\u23f0 Para: " + data["due_date"]
        await send_whatsapp(phone, msg)

    elif intent == "idea":
        iid = add_idea(data)
        await send_whatsapp(phone, "\U0001f4a1 Idea #" + str(iid) + " guardada\n_" + data.get("content", "") + "_")

    elif intent == "reminder":
        rid = add_reminder(data)
        await send_whatsapp(
            phone,
            "\u23f0 Recordatorio #" + str(rid) + "\n_" + data.get("message", "") + "_\n\U0001f550 " + data.get("remind_at", "")
        )

    elif intent == "query":
        qt = data.get("query_type", "pending_tasks")
        if qt in ["pending_tasks", "category"]:
            await send_whatsapp(phone, format_tasks(get_pending_tasks(data.get("category"))))
        elif qt == "ideas":
            await send_whatsapp(phone, format_ideas(get_recent_ideas()))
        elif qt == "today":
            await send_whatsapp(phone, format_summary(get_today_summary()))
        elif qt == "overdue":
            tasks = get_overdue_tasks()
            if tasks:
                await send_whatsapp(phone, "\u26a0\ufe0f *VENCIDAS*\n" + format_tasks(tasks))
            else:
                await send_whatsapp(phone, "\u2705 Nada vencido.")
        else:
            await send_whatsapp(phone, response_text)

    elif intent == "complete":
        search = data.get("search_term", "")
        found = complete_task(search)
        if found and found.startswith("MULTIPLE:"):
            await send_whatsapp(phone, "\U0001f914 Varias coinciden:\n" + found[9:] + "\n\nSe mas especifico.")
        elif found:
            await send_whatsapp(phone, "\U0001f389 *Completada:* " + found)
        else:
            await send_whatsapp(phone, "\U0001f50d No encontre esa tarea. Escribe *pendientes* para ver la lista.")

    else:
        await send_whatsapp(phone, response_text)


scheduler = AsyncIOScheduler(timezone=TIMEZONE)


async def check_reminders():
    for r in get_pending_reminders():
        if MY_PHONE_NUMBER:
            await send_whatsapp(MY_PHONE_NUMBER, "\u23f0 *RECORDATORIO*\n\n" + r["message"])
            mark_reminder_sent(r["id"])


async def morning_summary():
    if not MY_PHONE_NUMBER:
        return
    summary = get_today_summary()
    high = [t for t in get_pending_tasks() if t["priority"] == "alta"]
    msg = "\u2600\ufe0f *Buenos dias, Christian*\n\n" + format_summary(summary)
    if high:
        msg = msg + "\n\n\U0001f3af *FOCUS HOY:*"
        for t in high[:3]:
            msg = msg + "\n  " + C_EMOJI.get(t["category"], "\U0001f4cc") + " " + t["title"]
    await send_whatsapp(MY_PHONE_NUMBER, msg)


async def evening_review():
    if not MY_PHONE_NUMBER:
        return
    s = get_today_summary()
    msg = (
        "\U0001f319 *Cierre del dia*\n\n"
        "\u2705 Completaste *" + str(s["completed_today"]) + "* hoy\n"
        "\U0001f4cb Quedan *" + str(s["pending"]) + "* pendientes"
    )
    if s["overdue"]:
        msg = msg + "\n\u26a0\ufe0f *" + str(len(s["overdue"])) + "* vencidas"
    await send_whatsapp(MY_PHONE_NUMBER, msg)


app = FastAPI(title="Asistente Personal WhatsApp")


@app.on_event("startup")
async def startup():
    init_db()
    scheduler.add_job(check_reminders, IntervalTrigger(minutes=1), id="reminders")
    scheduler.add_job(morning_summary, CronTrigger(hour=7, minute=0), id="morning")
    scheduler.add_job(evening_review, CronTrigger(hour=21, minute=0), id="evening")
    scheduler.start()
    logger.info("Asistente Personal iniciado")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_token == VERIFY_TOKEN:
        logger.info("Webhook verificado")
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request):
    body = await request.json()
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                messages = change.get("value", {}).get("messages", [])
                for msg in messages:
                    if msg.get("type") == "text":
                        phone = msg["from"]
                        text = msg["text"]["body"]
                        logger.info("Mensaje de %s: %s", phone, text)
                        if MY_PHONE_NUMBER and phone != MY_PHONE_NUMBER:
                            logger.warning("Numero no autorizado: %s", phone)
                            continue
                        await process_message(phone, text)
    except Exception as e:
        logger.error("Webhook error: %s", e)
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "running", "time": datetime.now(tz).isoformat()}


@app.get("/tasks")
async def api_tasks():
    return get_pending_tasks()


@app.get("/ideas")
async def api_ideas():
    return get_recent_ideas(20)


@app.get("/summary")
async def api_summary():
    return get_today_summary()
