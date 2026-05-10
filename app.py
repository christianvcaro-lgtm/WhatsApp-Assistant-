import os
import json
import html as html_lib
import secrets
import httpx
import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import libsql_experimental as libsql
from fastapi import FastAPI, Request, Response, Query, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from openai import OpenAI

import gcal

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "653078644555574")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "mi_asistente_personal_2024")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MY_PHONE_NUMBER = os.environ.get("MY_PHONE_NUMBER", "")
TIMEZONE = os.environ.get("TIMEZONE", "America/Bogota")
TURSO_URL = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
tz = ZoneInfo(TIMEZONE)


def get_db():
    conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    return conn


def init_db():
    conn = get_db()
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
        "CREATE TABLE IF NOT EXISTS context ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "key TEXT NOT NULL,"
        "value TEXT NOT NULL,"
        "created_at TEXT DEFAULT (datetime('now')),"
        "updated_at TEXT DEFAULT (datetime('now')));"
        "CREATE TABLE IF NOT EXISTS conversations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "role TEXT NOT NULL,"
        "content TEXT NOT NULL,"
        "created_at TEXT DEFAULT (datetime('now')));"
        "CREATE TABLE IF NOT EXISTS calendar_notifications_sent ("
        "event_id TEXT,"
        "notification_type TEXT,"
        "sent_at TEXT DEFAULT (datetime('now')),"
        "PRIMARY KEY (event_id, notification_type));"
        "CREATE TABLE IF NOT EXISTS config ("
        "key TEXT PRIMARY KEY,"
        "value TEXT NOT NULL,"
        "updated_at TEXT DEFAULT (datetime('now')));"
    )
    conn.commit()


openai_client = OpenAI(api_key=OPENAI_API_KEY)


def get_all_context():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM context ORDER BY updated_at DESC").fetchall()
    result = {}
    for r in rows:
        result[r[0]] = r[1]
    return result


def set_context(key, value):
    conn = get_db()
    existing = conn.execute("SELECT id FROM context WHERE key=?", (key,)).fetchone()
    if existing:
        conn.execute("UPDATE context SET value=?, updated_at=datetime('now') WHERE key=?", (value, key))
    else:
        conn.execute("INSERT INTO context (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def get_recent_conversations(limit=20):
    conn = get_db()
    rows = conn.execute(
        "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    result = [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    return result


def save_conversation(role, content):
    conn = get_db()
    conn.execute("INSERT INTO conversations (role, content) VALUES (?, ?)", (role, content))
    conn.execute(
        "DELETE FROM conversations WHERE id NOT IN "
        "(SELECT id FROM conversations ORDER BY id DESC LIMIT 50)"
    )
    conn.commit()


def get_config(key, default=""):
    conn = get_db()
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_config(key, value):
    conn = get_db()
    existing = conn.execute("SELECT key FROM config WHERE key=?", (key,)).fetchone()
    if existing:
        conn.execute("UPDATE config SET value=?, updated_at=datetime('now') WHERE key=?", (value, key))
    else:
        conn.execute("INSERT INTO config (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def delete_config(key):
    conn = get_db()
    conn.execute("DELETE FROM config WHERE key=?", (key,))
    conn.commit()


DEFAULT_PROMPT_BODY = """Eres el asistente personal de Christian. No eres un bot generico, eres SU asistente. Conoces sus proyectos, sus prioridades, su forma de pensar. Te habla por WhatsApp en espanol colombiano informal.

QUIEN ES CHRISTIAN:
- Emprendedor colombiano con dos proyectos activos
- YAVE: CRM de WhatsApp con IA para inmobiliarias (en desarrollo y validacion)
- Los Lagos: desarrollo de lotes campestres cerca de Cartagena con financiamiento directo
- Es estratega, ejecutor, le gusta ir directo al grano
- Juega padel, vive en Colombia
{{CONTEXT_BLOCK}}

TU PERSONALIDAD:
- Eres directo, inteligente, y genuinamente util. No eres lambiscone.
- Hablas como un socio de confianza, no como un chatbot corporativo.
- Si Christian esta disperso o haciendo mucho, se lo dices.
- Si una idea no tiene sentido, cuestionala con respeto pero sin miedo.
- Ayudas a PRIORIZAR, no solo a guardar cosas. Eso es clave.
- Respondes conciso porque esto es WhatsApp. Nada de parrafos largos.
- Puedes usar emojis con moderacion.
- Si no entiendes algo, preguntas. No asumes.

QUE PUEDES HACER:
1. Guardar tareas, ideas, recordatorios
2. Dar resumen del dia, pendientes, ideas
3. Recordar informacion personal que Christian te ensene
4. Ayudar a pensar, priorizar, decidir
5. Tener conversaciones normales como un asistente real
6. Cuestionar cuando algo no tiene sentido
7. Agendar eventos en Google Calendar y avisar antes de cada uno

COMO RESPONDER:
Responde SIEMPRE en JSON valido. Sin markdown, sin backticks.

Estructura:
{"intent": "TIPO", "data": {...}, "response": "Tu respuesta para WhatsApp"}

INTENTS POSIBLES:
- task: cuando quiere agregar algo que HACER (detecta: "tengo que", "necesito", "hay que", "pendiente", "hacer", "tarea")
- idea: cuando tiene una IDEA (detecta: "idea", "se me ocurrio", "que tal si", "podriamos")
- reminder: cuando quiere un RECORDATORIO (detecta: "recuerdame", "no se me olvide", "avisame", "a las X")
- query: cuando PREGUNTA por sus cosas (detecta: "que tengo", "que tareas tengo", "que pendientes tengo", "pendientes", "resumen", "como voy", "mis tareas", "muestrame mis", "muestra mis", "lista", "cuales son", "que hay")
- complete: cuando COMPLETO algo (detecta: "listo", "hecho", "ya hice", "termine")
- learn: cuando te ENSENA algo sobre el o sus proyectos (detecta: "recuerda que", "mi prioridad es", "ten en cuenta", "aprende que", "esto es importante")
- agendar_evento: cuando quiere AGENDAR algo en su CALENDARIO de Google (detecta: "agendame", "agrega al calendario", "pon una reunion", "tengo una cita", "reserva X dia"). Distinto de reminder: agendar_evento crea evento en Google Calendar; reminder es solo un mensaje.
- chat: conversacion normal, consejo, ayuda para pensar

DATA POR INTENT:
task: {"title":"corto","description":"detalle o null","priority":"alta|media|baja","category":"yave|loslagos|personal|general","due_date":"YYYY-MM-DD o null"}
idea: {"content":"la idea completa","category":"yave|loslagos|personal|general","tags":["tag1"]}
reminder: {"message":"que recordar","remind_at":"YYYY-MM-DD HH:MM"}
query: {"query_type":"pending_tasks|ideas|today|overdue|category","category":"DEBE ser null por DEFAULT. Solo poner yave|loslagos|personal|general SI la pregunta menciona EXPLICITAMENTE ese proyecto. Ejemplo: 'que tareas tengo' -> category null. 'que tareas tengo de yave' -> category yave."}
complete: {"search_term":"texto para buscar la tarea"}
learn: {"key":"tema corto","value":"lo que debe recordar"}
agendar_evento: {"title":"corto","start_date":"YYYY-MM-DD","start_time":"HH:MM","duration_minutes":60,"description":"opcional o null"}
chat: {}

REGLAS DE PRIORIDAD:
- urgente/hoy/asap = alta
- fecha < 3 dias = alta
- sin fecha ni urgencia = media
- cuando pueda/algun dia = baja

REGLAS DE CATEGORIA:
- WhatsApp, CRM, API, Meta, codigo, tech, desarrollo = yave
- lotes, Cartagena, ventas, financiamiento, campestre = loslagos
- gym, padel, familia, salud = personal
- resto = general

REGLAS DE INTELIGENCIA:
- Si te dice algo vago como 'tengo que hacer cosas de YAVE', preguntale QUE cosas especificamente.
- Si agrega una tarea baja cuando tiene 5 altas pendientes, dile algo como 'ojo que tienes 5 urgentes, seguro quieres agregar mas?'
- Si no ha completado tareas en un rato, motivalo o preguntale que esta pasando.
- Si una idea se repite o contradice algo anterior, mencionalo.
- En el chat, se genuinamente util. Ayuda a pensar, no solo a responder.
- IMPORTANTE: en tu response, incluye la confirmacion de la accion Y cualquier comentario inteligente que tengas.
{{TASKS_BLOCK}}{{IDEAS_BLOCK}}

FECHA: {{CURRENT_DATE}} ({{DAY_NAME}}) | HORA: {{CURRENT_TIME}}

Para reminders: calcula fecha/hora real. 'manana a las 8' = fecha de manana 08:00. 'en 2 horas' = suma desde hora actual."""


def build_system_prompt():
    now = datetime.now(tz)
    current_date = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")
    day_name = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"][now.weekday()]

    ctx = get_all_context()
    context_block = ""
    if ctx:
        context_block = "\n\nCONOCIMIENTO PERSONAL DE CHRISTIAN:\n"
        for k, v in ctx.items():
            context_block = context_block + "- " + k + ": " + v + "\n"

    tasks = get_pending_tasks()
    tasks_block = ""
    if tasks:
        tasks_block = "\n\nTAREAS PENDIENTES ACTUALES (" + str(len(tasks)) + "):\n"
        for t in tasks[:10]:
            due = " [vence: " + t["due_date"] + "]" if t.get("due_date") else ""
            tasks_block = tasks_block + "- [" + t["priority"] + "] [" + t["category"] + "] " + t["title"] + due + "\n"
        if len(tasks) > 10:
            tasks_block = tasks_block + "... y " + str(len(tasks) - 10) + " mas\n"

    ideas = get_recent_ideas(5)
    ideas_block = ""
    if ideas:
        ideas_block = "\n\nIDEAS RECIENTES:\n"
        for i in ideas:
            ideas_block = ideas_block + "- [" + i["category"] + "] " + i["content"] + "\n"

    body = get_config("system_prompt", DEFAULT_PROMPT_BODY)
    return (body
        .replace("{{CONTEXT_BLOCK}}", context_block)
        .replace("{{TASKS_BLOCK}}", tasks_block)
        .replace("{{IDEAS_BLOCK}}", ideas_block)
        .replace("{{CURRENT_DATE}}", current_date)
        .replace("{{DAY_NAME}}", day_name)
        .replace("{{CURRENT_TIME}}", current_time)
    )


async def interpret_message(text):
    prompt = build_system_prompt()
    messages = [{"role": "system", "content": prompt}]

    history = get_recent_conversations(10)
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    try:
        response = openai_client.chat.completions.create(
            model="gpt-5.4-mini",
            max_completion_tokens=1024,
            temperature=0.7,
            messages=messages
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)

        save_conversation("user", text)
        save_conversation("assistant", parsed.get("response", ""))

        return parsed
    except json.JSONDecodeError:
        logger.error("JSON parse error. Raw: %s", raw)
        save_conversation("user", text)
        return {"intent": "chat", "data": {}, "response": "No entendi bien, puedes reformularlo?"}
    except Exception as e:
        logger.error("OpenAI error: %s", e)
        return {"intent": "chat", "data": {}, "response": "Error procesando. Intenta de nuevo."}


def add_task(data):
    conn = get_db()
    conn.execute(
        "INSERT INTO tasks (title,description,priority,category,due_date) VALUES (?,?,?,?,?)",
        (data.get("title", "Sin titulo"), data.get("description"),
         data.get("priority", "media"), data.get("category", "general"), data.get("due_date"))
    )
    conn.commit()
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    return row[0]


def add_idea(data):
    conn = get_db()
    conn.execute(
        "INSERT INTO ideas (content,category,tags) VALUES (?,?,?)",
        (data.get("content", ""), data.get("category", "general"), json.dumps(data.get("tags", [])))
    )
    conn.commit()
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    return row[0]


def add_reminder(data):
    conn = get_db()
    conn.execute(
        "INSERT INTO reminders (message,remind_at) VALUES (?,?)",
        (data.get("message", ""), data.get("remind_at", ""))
    )
    conn.commit()
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    return row[0]


def complete_task(search_term):
    conn = get_db()
    tasks = conn.execute(
        "SELECT id,title FROM tasks WHERE status='pendiente' AND LOWER(title) LIKE ?",
        ("%" + search_term.lower() + "%",)
    ).fetchall()
    if len(tasks) == 1:
        conn.execute(
            "UPDATE tasks SET status='completada',completed_at=datetime('now') WHERE id=?",
            (tasks[0][0],)
        )
        conn.commit()
        return tasks[0][1]
    elif len(tasks) > 1:
        return "MULTIPLE:" + ", ".join(t[1] for t in tasks)
    return None


def get_pending_tasks(category=None):
    conn = get_db()
    if category and category != "null":
        rows = conn.execute(
            "SELECT id,title,description,priority,category,due_date,status,created_at,completed_at "
            "FROM tasks WHERE status='pendiente' AND category=? "
            "ORDER BY CASE priority WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END, due_date",
            (category,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id,title,description,priority,category,due_date,status,created_at,completed_at "
            "FROM tasks WHERE status='pendiente' "
            "ORDER BY CASE priority WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END, due_date"
        ).fetchall()
    result = []
    for r in rows:
        result.append({"id": r[0], "title": r[1], "description": r[2], "priority": r[3],
                        "category": r[4], "due_date": r[5], "status": r[6],
                        "created_at": r[7], "completed_at": r[8]})
    return result


def get_overdue_tasks():
    today = datetime.now(tz).strftime("%Y-%m-%d")
    conn = get_db()
    rows = conn.execute(
        "SELECT id,title,description,priority,category,due_date,status,created_at,completed_at "
        "FROM tasks WHERE status='pendiente' AND due_date<? AND due_date IS NOT NULL",
        (today,)
    ).fetchall()
    result = []
    for r in rows:
        result.append({"id": r[0], "title": r[1], "description": r[2], "priority": r[3],
                        "category": r[4], "due_date": r[5], "status": r[6],
                        "created_at": r[7], "completed_at": r[8]})
    return result


def get_recent_ideas(limit=10):
    conn = get_db()
    rows = conn.execute(
        "SELECT id,content,category,tags,created_at,reviewed FROM ideas ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    result = []
    for r in rows:
        result.append({"id": r[0], "content": r[1], "category": r[2],
                        "tags": r[3], "created_at": r[4], "reviewed": r[5]})
    return result


def get_today_summary():
    today = datetime.now(tz).strftime("%Y-%m-%d")
    conn = get_db()
    pending = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='pendiente'"
    ).fetchone()[0]
    high = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='pendiente' AND priority='alta'"
    ).fetchone()[0]
    due_today_rows = conn.execute(
        "SELECT id,title,description,priority,category,due_date FROM tasks WHERE status='pendiente' AND due_date=?",
        (today,)
    ).fetchall()
    overdue_rows = conn.execute(
        "SELECT id,title,description,priority,category,due_date FROM tasks WHERE status='pendiente' AND due_date<? AND due_date IS NOT NULL",
        (today,)
    ).fetchall()
    completed = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE completed_at LIKE ?", (today + "%",)
    ).fetchone()[0]
    ideas_count = conn.execute(
        "SELECT COUNT(*) FROM ideas WHERE created_at LIKE ?", (today + "%",)
    ).fetchone()[0]
    due_today = [{"title": r[1], "due_date": r[5]} for r in due_today_rows]
    overdue = [{"title": r[1], "due_date": r[5]} for r in overdue_rows]
    return {
        "pending": pending, "high_priority": high,
        "due_today": due_today, "overdue": overdue,
        "completed_today": completed, "ideas_today": ideas_count
    }


def get_pending_reminders():
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    conn = get_db()
    rows = conn.execute(
        "SELECT id,message,remind_at FROM reminders WHERE sent=0 AND remind_at<=?", (now,)
    ).fetchall()
    return [{"id": r[0], "message": r[1], "remind_at": r[2]} for r in rows]


def mark_reminder_sent(rid):
    conn = get_db()
    conn.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
    conn.commit()


def is_calendar_notification_sent(event_id, notif_type):
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM calendar_notifications_sent WHERE event_id=? AND notification_type=?",
        (event_id, notif_type)
    ).fetchone()
    return row is not None


def mark_calendar_notification_sent(event_id, notif_type):
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO calendar_notifications_sent (event_id, notification_type) VALUES (?, ?)",
        (event_id, notif_type)
    )
    conn.commit()


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


async def transcribe_audio(media_id):
    async with httpx.AsyncClient() as http:
        resp = await http.get(
            "https://graph.facebook.com/v22.0/" + media_id,
            headers={"Authorization": "Bearer " + WHATSAPP_TOKEN},
            timeout=10
        )
        media_url = resp.json().get("url", "")
        audio_resp = await http.get(
            media_url,
            headers={"Authorization": "Bearer " + WHATSAPP_TOKEN},
            timeout=30
        )
        audio_bytes = audio_resp.content
    audio_file = ("audio.ogg", audio_bytes, "audio/ogg")
    transcript = openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file
    )
    return transcript.text


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
            "\U0001f916 *SOY TU ASISTENTE PERSONAL*\n\n"
            "*Comandos rapidos:*\n"
            "\U0001f4cb *pendientes* = ver tareas\n"
            "\U0001f4a1 *ideas* = ver ideas\n"
            "\U0001f4ca *resumen* = resumen del dia\n\n"
            "*Hablame natural:*\n"
            "- _Tengo que llamar a Juan manana_\n"
            "- _Idea: hacer webinar de YAVE_\n"
            "- _Recuerdame a las 3pm revisar metricas_\n"
            "- _Ya hice lo de Juan_\n\n"
            "*Ensenami cosas:*\n"
            "- _Recuerda que mi prioridad es cerrar 3 ventas_\n"
            "- _Ten en cuenta que el lanzamiento es en abril_\n\n"
            "*Tambien puedes enviarme notas de voz!*"
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
        if response_text and response_text != data.get("title", ""):
            msg = msg + "\n\n" + response_text
        await send_whatsapp(phone, msg)

    elif intent == "idea":
        iid = add_idea(data)
        msg = "\U0001f4a1 Idea #" + str(iid) + " guardada\n_" + data.get("content", "") + "_"
        if response_text and response_text != data.get("content", ""):
            msg = msg + "\n\n" + response_text
        await send_whatsapp(phone, msg)

    elif intent == "reminder":
        rid = add_reminder(data)
        msg = "\u23f0 Recordatorio #" + str(rid) + "\n_" + data.get("message", "") + "_\n\U0001f550 " + data.get("remind_at", "")
        if response_text:
            msg = msg + "\n\n" + response_text
        await send_whatsapp(phone, msg)

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
            msg = "\U0001f389 *Completada:* " + found
            if response_text:
                msg = msg + "\n\n" + response_text
            await send_whatsapp(phone, msg)
        else:
            await send_whatsapp(phone, "\U0001f50d No encontre esa tarea. Escribe *pendientes* para ver la lista.")

    elif intent == "learn":
        key = data.get("key", "")
        value = data.get("value", "")
        if key and value:
            set_context(key, value)
            await send_whatsapp(phone, "\U0001f9e0 Listo, me lo guarde.\n\n" + response_text)
        else:
            await send_whatsapp(phone, response_text)

    elif intent == "agendar_evento":
        if not gcal.is_configured():
            await send_whatsapp(phone, "Google Calendar no esta configurado todavia.")
        else:
            title = data.get("title", "")
            start_date = data.get("start_date", "")
            start_time = data.get("start_time", "")
            duration_min = data.get("duration_minutes") or 60
            description = data.get("description")
            if not title or not start_date or not start_time:
                await send_whatsapp(phone, "No entendi bien la fecha/hora del evento. Reformulalo?")
            else:
                try:
                    start_dt = datetime.strptime(start_date + " " + start_time, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                    end_dt = start_dt + timedelta(minutes=int(duration_min))
                    event = gcal.create_event(title, start_dt.isoformat(), end_dt.isoformat(), description)
                    if event:
                        await send_whatsapp(phone, gcal.format_event_for_creation(event))
                    else:
                        await send_whatsapp(phone, "Hubo un error creando el evento. Intenta de nuevo.")
                except Exception as e:
                    logger.error("Error procesando agendar_evento: %s", e)
                    await send_whatsapp(phone, "Hubo un error con la fecha/hora. Intenta de nuevo.")

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
    ctx = get_all_context()

    msg = "\u2600\ufe0f *Buenos dias, Christian*\n\n" + format_summary(summary)

    if high:
        msg = msg + "\n\n\U0001f3af *FOCUS HOY:*"
        for t in high[:3]:
            msg = msg + "\n  " + C_EMOJI.get(t["category"], "\U0001f4cc") + " " + t["title"]

    priority = ctx.get("prioridad_semana", ctx.get("prioridad", ""))
    if priority:
        msg = msg + "\n\n\U0001f4ad Recuerda: " + priority

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
        msg = msg + "\n\u26a0\ufe0f *" + str(len(s["overdue"])) + "* vencidas - no las pierdas de vista"
    await send_whatsapp(MY_PHONE_NUMBER, msg)


async def check_calendar_events():
    if not gcal.is_configured() or not MY_PHONE_NUMBER:
        return
    events = gcal.list_upcoming_events(minutes_ahead=35)
    now = datetime.now(tz)
    for event in events:
        event_id = event.get("id", "")
        if not event_id:
            continue
        start_str = event.get("start", {}).get("dateTime", "")
        try:
            start_dt = datetime.fromisoformat(start_str)
        except Exception:
            continue
        delta_min = (start_dt - now).total_seconds() / 60

        if 25 <= delta_min <= 30 and not is_calendar_notification_sent(event_id, "T-30"):
            await send_whatsapp(MY_PHONE_NUMBER, gcal.format_event_for_reminder(event, "T-30"))
            mark_calendar_notification_sent(event_id, "T-30")
        if 10 <= delta_min <= 15 and not is_calendar_notification_sent(event_id, "T-15"):
            await send_whatsapp(MY_PHONE_NUMBER, gcal.format_event_for_reminder(event, "T-15"))
            mark_calendar_notification_sent(event_id, "T-15")
        if -5 <= delta_min <= 1 and not is_calendar_notification_sent(event_id, "T-0"):
            await send_whatsapp(MY_PHONE_NUMBER, gcal.format_event_for_reminder(event, "T-0"))
            mark_calendar_notification_sent(event_id, "T-0")


app = FastAPI(title="Asistente Personal WhatsApp v3")


@app.on_event("startup")
async def startup():
    init_db()
    scheduler.add_job(check_reminders, IntervalTrigger(minutes=1), id="reminders")
    scheduler.add_job(morning_summary, CronTrigger(hour=7, minute=0), id="morning")
    scheduler.add_job(evening_review, CronTrigger(hour=21, minute=0), id="evening")
    if gcal.is_configured():
        scheduler.add_job(check_calendar_events, IntervalTrigger(minutes=5), id="gcal")
        logger.info("Google Calendar: configurado")
    else:
        logger.warning("Google Calendar: NO configurado (faltan env vars GOOGLE_*)")
    scheduler.start()
    logger.info("Asistente Personal v3 iniciado - Turso DB")


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
                    phone = msg["from"]
                    if MY_PHONE_NUMBER and phone != MY_PHONE_NUMBER:
                        logger.warning("Numero no autorizado: %s", phone)
                        continue
                    if msg.get("type") == "text":
                        text = msg["text"]["body"]
                        logger.info("Mensaje de %s: %s", phone, text)
                        await process_message(phone, text)
                    elif msg.get("type") == "audio":
                        logger.info("Audio de %s", phone)
                        try:
                            media_id = msg["audio"]["id"]
                            text = await transcribe_audio(media_id)
                            logger.info("Transcripcion: %s", text)
                            await process_message(phone, text)
                        except Exception as e:
                            logger.error("Error transcribiendo audio: %s", e)
                            await send_whatsapp(phone, "No pude entender el audio. Intenta de nuevo o escribeme.")
    except Exception as e:
        logger.error("Webhook error: %s", e)
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "running", "version": "v3-turso", "time": datetime.now(tz).isoformat()}


@app.get("/tasks")
async def api_tasks():
    return get_pending_tasks()


@app.get("/ideas")
async def api_ideas():
    return get_recent_ideas(20)


@app.get("/summary")
async def api_summary():
    return get_today_summary()


@app.get("/context")
async def api_context():
    return get_all_context()


admin_security = HTTPBasic()


def admin_auth(credentials: HTTPBasicCredentials = Depends(admin_security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD no configurado en Railway")
    if not secrets.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Basic"})
    return True


ADMIN_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Asistente - Editar Prompt</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 1000px; margin: 20px auto; padding: 20px; background: #f5f5f7; color: #1d1d1f; }
        h1 { margin-bottom: 4px; }
        .sub { color: #6e6e73; margin-bottom: 20px; }
        .info { padding: 12px; border-radius: 8px; margin: 12px 0; font-size: 14px; }
        .info-default { background: #d1ecf1; border: 1px solid #17a2b8; }
        .info-custom { background: #fff3cd; border: 1px solid #ffc107; }
        .markers { background: white; border: 1px solid #d2d2d7; padding: 14px 16px; border-radius: 8px; margin: 12px 0; font-size: 13px; line-height: 1.7; }
        textarea { width: 100%; height: 600px; font-family: "Menlo", "Monaco", monospace; font-size: 13px; padding: 12px; border: 1px solid #d2d2d7; border-radius: 8px; resize: vertical; box-sizing: border-box; line-height: 1.5; }
        .btn-row { margin: 16px 0; display: flex; gap: 12px; flex-wrap: wrap; }
        button { padding: 12px 24px; font-size: 16px; cursor: pointer; border: none; border-radius: 8px; font-weight: 500; }
        .btn-save { background: #007aff; color: white; }
        .btn-save:hover { background: #0056d3; }
        .btn-reset { background: #f5f5f7; color: #1d1d1f; border: 1px solid #d2d2d7; }
        .btn-reset:hover { background: #e8e8ed; }
        #status { padding: 12px; margin: 12px 0; border-radius: 8px; min-height: 20px; font-size: 14px; }
        .status-ok { background: #d4edda; color: #155724; }
        .status-err { background: #f8d7da; color: #721c24; }
        .status-loading { background: #fff3cd; color: #856404; }
        code { background: #f0f0f0; padding: 2px 6px; border-radius: 3px; font-family: "Menlo", monospace; font-size: 12px; }
    </style>
</head>
<body>
    <h1>Editor del Prompt</h1>
    <p class="sub">El bot usa el nuevo prompt en tiempo real apenas guardas. Sin redeploy.</p>

    __BANNER__

    <div class="markers">
        <strong>Marcadores que el bot reemplaza solo:</strong><br>
        <code>{{CONTEXT_BLOCK}}</code> tu contexto guardado &nbsp;
        <code>{{TASKS_BLOCK}}</code> tus tareas pendientes &nbsp;
        <code>{{IDEAS_BLOCK}}</code> tus ideas recientes<br>
        <code>{{CURRENT_DATE}}</code> &nbsp;
        <code>{{DAY_NAME}}</code> &nbsp;
        <code>{{CURRENT_TIME}}</code><br>
        Si quitas un marcador, simplemente esa info no se inyecta. No falla.
    </div>

    <textarea id="prompt">__PROMPT__</textarea>

    <div class="btn-row">
        <button class="btn-save" onclick="save()">Guardar</button>
        <button class="btn-reset" onclick="resetDefault()">Volver al prompt default</button>
    </div>

    <div id="status"></div>

    <script>
        function setStatus(text, kind) {
            const s = document.getElementById("status");
            s.textContent = text;
            s.className = "status-" + kind;
        }
        async function save() {
            setStatus("Guardando...", "loading");
            const text = document.getElementById("prompt").value;
            try {
                const r = await fetch("/admin/prompt", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({prompt: text})
                });
                if (r.ok) {
                    setStatus("Guardado. El bot ya usa el prompt nuevo.", "ok");
                } else {
                    setStatus("Error " + r.status + ": " + await r.text(), "err");
                }
            } catch(e) {
                setStatus("Error de red: " + e.message, "err");
            }
        }
        async function resetDefault() {
            if (!confirm("Volver al prompt default? Tu version guardada se borra (no se puede deshacer).")) return;
            setStatus("Reseteando...", "loading");
            try {
                const r = await fetch("/admin/prompt/reset", {method: "POST"});
                if (r.ok) {
                    setStatus("Reseteado. Recarga la pagina para ver el default.", "ok");
                } else {
                    setStatus("Error " + r.status, "err");
                }
            } catch(e) {
                setStatus("Error de red: " + e.message, "err");
            }
        }
    </script>
</body>
</html>"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(_: bool = Depends(admin_auth)):
    saved = get_config("system_prompt")
    using_default = not bool(saved)
    current = saved if saved else DEFAULT_PROMPT_BODY
    banner = (
        '<div class="info info-default">Estas viendo el <strong>prompt default</strong> (no hay version personalizada en la DB todavia).</div>'
        if using_default
        else '<div class="info info-custom">Estas editando una version <strong>personalizada</strong> guardada en la DB.</div>'
    )
    html = (ADMIN_HTML_TEMPLATE
        .replace("__BANNER__", banner)
        .replace("__PROMPT__", html_lib.escape(current))
    )
    return HTMLResponse(html)


@app.get("/admin/prompt")
async def admin_get_prompt(_: bool = Depends(admin_auth)):
    saved = get_config("system_prompt")
    return {
        "prompt": saved if saved else DEFAULT_PROMPT_BODY,
        "using_default": not bool(saved),
    }


@app.post("/admin/prompt")
async def admin_save_prompt(request: Request, _: bool = Depends(admin_auth)):
    body = await request.json()
    new_prompt = body.get("prompt", "")
    if not new_prompt or not new_prompt.strip():
        raise HTTPException(400, "Prompt vacio")
    set_config("system_prompt", new_prompt)
    return {"status": "ok"}


@app.post("/admin/prompt/reset")
async def admin_reset_prompt(_: bool = Depends(admin_auth)):
    delete_config("system_prompt")
    return {"status": "ok"}
