import os
import json
import sqlite3
import httpx
import base64
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
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
VAULT_REPO = os.environ.get("VAULT_REPO", "christianvcaro-lgtm/obsidian-vault")

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
        )


openai_client = OpenAI(api_key=OPENAI_API_KEY)


def get_all_context():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM context ORDER BY updated_at DESC").fetchall()
        return {r["key"]: r["value"] for r in rows}


def set_context(key, value):
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM context WHERE key=?", (key,)).fetchone()
        if existing:
            conn.execute("UPDATE context SET value=?, updated_at=datetime('now') WHERE key=?", (value, key))
        else:
            conn.execute("INSERT INTO context (key, value) VALUES (?, ?)", (key, value))


def get_recent_conversations(limit=20):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def save_conversation(role, content):
    with get_db() as conn:
        conn.execute("INSERT INTO conversations (role, content) VALUES (?, ?)", (role, content))
        conn.execute(
            "DELETE FROM conversations WHERE id NOT IN "
            "(SELECT id FROM conversations ORDER BY id DESC LIMIT 50)"
        )


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

    prompt = (
        "Eres el asistente personal de Christian. No eres un bot generico, eres SU asistente. "
        "Conoces sus proyectos, sus prioridades, su forma de pensar. "
        "Te habla por WhatsApp en espanol colombiano informal.\n\n"

        "QUIEN ES CHRISTIAN:\n"
        "- Emprendedor colombiano con dos proyectos activos\n"
        "- YAVE: CRM de WhatsApp con IA para inmobiliarias (en desarrollo y validacion)\n"
        "- Los Lagos: desarrollo de lotes campestres cerca de Cartagena con financiamiento directo\n"
        "- Es estratega, ejecutor, le gusta ir directo al grano\n"
        "- Juega padel, vive en Colombia\n"
        + context_block +

        "\n\nTU PERSONALIDAD:\n"
        "- Eres directo, inteligente, y genuinamente util. No eres lambiscone.\n"
        "- Hablas como un socio de confianza, no como un chatbot corporativo.\n"
        "- Si Christian esta disperso o haciendo mucho, se lo dices.\n"
        "- Si una idea no tiene sentido, cuestionala con respeto pero sin miedo.\n"
        "- Ayudas a PRIORIZAR, no solo a guardar cosas. Eso es clave.\n"
        "- Respondes conciso porque esto es WhatsApp. Nada de parrafos largos.\n"
        "- Puedes usar emojis con moderacion.\n"
        "- Si no entiendes algo, preguntas. No asumes.\n"

        "\n\nQUE PUEDES HACER:\n"
        "1. Guardar tareas, ideas, recordatorios\n"
        "2. Dar resumen del dia, pendientes, ideas\n"
        "3. Recordar informacion personal que Christian te ensene\n"
        "4. Ayudar a pensar, priorizar, decidir\n"
        "5. Tener conversaciones normales como un asistente real\n"
        "6. Cuestionar cuando algo no tiene sentido\n"

        "\n\nCOMO RESPONDER:\n"
        "Responde SIEMPRE en JSON valido. Sin markdown, sin backticks.\n\n"
        "Estructura:\n"
        '{"intent": "TIPO", "data": {...}, "response": "Tu respuesta para WhatsApp"}\n\n'

        "INTENTS POSIBLES:\n"
        '- task: cuando quiere agregar algo que HACER (detecta: "tengo que", "necesito", "hay que", "pendiente", "hacer", "tarea")\n'
        '- idea: cuando tiene una IDEA (detecta: "idea", "se me ocurrio", "que tal si", "podriamos")\n'
        '- reminder: cuando quiere un RECORDATORIO (detecta: "recuerdame", "no se me olvide", "avisame", "a las X")\n'
        '- query: cuando PREGUNTA por sus cosas (detecta: "que tengo", "pendientes", "resumen", "como voy", "mis tareas")\n'
        '- complete: cuando COMPLETO algo (detecta: "listo", "hecho", "ya hice", "termine")\n'
        '- learn: cuando te ENSENA algo sobre el o sus proyectos (detecta: "recuerda que", "mi prioridad es", "ten en cuenta", "aprende que", "esto es importante")\n'
        '- chat: conversacion normal, consejo, ayuda para pensar\n\n'

        "DATA POR INTENT:\n"
        'task: {"title":"corto","description":"detalle o null","priority":"alta|media|baja","category":"yave|loslagos|personal|general","due_date":"YYYY-MM-DD o null"}\n'
        'idea: {"content":"la idea completa","category":"yave|loslagos|personal|general","tags":["tag1"]}\n'
        'reminder: {"message":"que recordar","remind_at":"YYYY-MM-DD HH:MM"}\n'
        'query: {"query_type":"pending_tasks|ideas|today|overdue|category","category":"opcional"}\n'
        'complete: {"search_term":"texto para buscar la tarea"}\n'
        'learn: {"key":"tema corto","value":"lo que debe recordar"}\n'
        'chat: {}\n\n'

        "REGLAS DE PRIORIDAD:\n"
        "- urgente/hoy/asap = alta\n"
        "- fecha < 3 dias = alta\n"
        "- sin fecha ni urgencia = media\n"
        "- cuando pueda/algun dia = baja\n\n"

        "REGLAS DE CATEGORIA:\n"
        "- WhatsApp, CRM, API, Meta, codigo, tech, desarrollo = yave\n"
        "- lotes, Cartagena, ventas, financiamiento, campestre = loslagos\n"
        "- gym, padel, familia, salud = personal\n"
        "- resto = general\n\n"

        "REGLAS DE INTELIGENCIA:\n"
        "- Si te dice algo vago como 'tengo que hacer cosas de YAVE', preguntale QUE cosas especificamente.\n"
        "- Si agrega una tarea baja cuando tiene 5 altas pendientes, dile algo como 'ojo que tienes 5 urgentes, seguro quieres agregar mas?'\n"
        "- Si no ha completado tareas en un rato, motivalo o preguntale que esta pasando.\n"
        "- Si una idea se repite o contradice algo anterior, mencionalo.\n"
        "- En el chat, se genuinamente util. Ayuda a pensar, no solo a responder.\n"
        "- IMPORTANTE: en tu response, incluye la confirmacion de la accion Y cualquier comentario inteligente que tengas.\n"
        + tasks_block
        + ideas_block +

        "\n\nFECHA: " + current_date + " (" + day_name + ") | HORA: " + current_time + "\n\n"
        "Para reminders: calcula fecha/hora real. "
        "'manana a las 8' = fecha de manana 08:00. "
        "'en 2 horas' = suma desde hora actual."
    )
    return prompt


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


async def push_to_vault(content: str, folder: str, filename: str):
    if not GITHUB_TOKEN:
        return
    url = f"https://api.github.com/repos/{VAULT_REPO}/contents/{folder}/{filename}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    encoded = base64.b64encode(content.encode()).decode()
    async with httpx.AsyncClient() as http:
        try:
            await http.put(url, headers=headers, json={
                "message": f"capture: {filename}",
                "content": encoded
            }, timeout=10)
            logger.info("Vault: nota guardada en %s/%s", folder, filename)
        except Exception as e:
            logger.error("Error escribiendo en vault: %s", e)


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
        fecha = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        md = (
            "---\n"
            "fecha: " + datetime.now(tz).strftime("%Y-%m-%d") + "\n"
            "tipo: tarea\n"
            "prioridad: " + data.get("priority", "media") + "\n"
            "proyecto: " + data.get("category", "general") + "\n"
            + ("vence: " + data["due_date"] + "\n" if data.get("due_date") else "") +
            "procesado: false\n"
            "---\n\n"
            "# " + data.get("title", "") + "\n\n"
            + (data.get("description", "") + "\n\n" if data.get("description") else "") +
            "---\n"
            "*Capturado desde WhatsApp el " + fecha + "*\n"
        )
        fname = datetime.now(tz).strftime("%Y-%m-%d-%H%M") + "-tarea-" + str(tid) + ".md"
        await push_to_vault(md, "inbox", fname)

    elif intent == "idea":
        iid = add_idea(data)
        msg = "\U0001f4a1 Idea #" + str(iid) + " guardada\n_" + data.get("content", "") + "_"
        if response_text and response_text != data.get("content", ""):
            msg = msg + "\n\n" + response_text
        await send_whatsapp(phone, msg)
        fecha = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        md = (
            "---\n"
            "fecha: " + datetime.now(tz).strftime("%Y-%m-%d") + "\n"
            "tipo: idea\n"
            "proyecto: " + data.get("category", "general") + "\n"
            "procesado: false\n"
            "---\n\n"
            "# " + data.get("content", "")[:60] + "\n\n"
            + data.get("content", "") + "\n\n"
            "---\n"
            "*Capturado desde WhatsApp el " + fecha + "*\n"
        )
        fname = datetime.now(tz).strftime("%Y-%m-%d-%H%M") + "-idea-" + str(iid) + ".md"
        await push_to_vault(md, "inbox", fname)

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


app = FastAPI(title="Asistente Personal WhatsApp v2")


@app.on_event("startup")
async def startup():
    init_db()
    scheduler.add_job(check_reminders, IntervalTrigger(minutes=1), id="reminders")
    scheduler.add_job(morning_summary, CronTrigger(hour=7, minute=0), id="morning")
    scheduler.add_job(evening_review, CronTrigger(hour=21, minute=0), id="evening")
    scheduler.start()
    logger.info("Asistente Personal v2 iniciado")


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
    return {"status": "running", "version": "v2", "time": datetime.now(tz).isoformat()}


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
