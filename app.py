import os
import json
import html as html_lib
import secrets
import asyncio
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
import gmail
import obsidian

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
        "CREATE TABLE IF NOT EXISTS memories ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "role TEXT NOT NULL,"
        "content TEXT NOT NULL,"
        "embedding TEXT NOT NULL,"
        "created_at TEXT DEFAULT (datetime('now')));"
    )
    conn.commit()


def _column_exists(conn, table, column):
    rows = conn.execute("PRAGMA table_info(" + table + ")").fetchall()
    return any(r[1] == column for r in rows)


def migrate_schema():
    conn = get_db()
    new_task_columns = [
        ("nudge_count", "INTEGER DEFAULT 0"),
        ("last_nudged_at", "TEXT"),
        ("postpone_count", "INTEGER DEFAULT 0"),
        ("snoozed_until", "TEXT"),
        ("source_event_id", "TEXT"),
    ]
    for col_name, col_def in new_task_columns:
        if not _column_exists(conn, "tasks", col_name):
            conn.execute("ALTER TABLE tasks ADD COLUMN " + col_name + " " + col_def)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS event_followups ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "event_id TEXT NOT NULL,"
        "event_title TEXT,"
        "event_end_at TEXT NOT NULL,"
        "scheduled_at TEXT NOT NULL,"
        "sent_at TEXT,"
        "responded_at TEXT,"
        "status TEXT DEFAULT 'pending',"
        "outcome TEXT,"
        "created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS processed_emails ("
        "message_id TEXT PRIMARY KEY,"
        "from_email TEXT,"
        "subject TEXT,"
        "important INTEGER DEFAULT 0,"
        "reason TEXT,"
        "notified INTEGER DEFAULT 0,"
        "created_at TEXT DEFAULT (datetime('now')))"
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
    save_memory(role, content)


def embed_text(text):
    try:
        resp = openai_client.embeddings.create(
            input=text[:8000],
            model="text-embedding-3-small"
        )
        return resp.data[0].embedding
    except Exception as e:
        logger.error("Embedding error: %s", e)
        return None


def save_memory(role, content):
    if not content or not content.strip():
        return
    emb = embed_text(content)
    if emb is None:
        return
    conn = get_db()
    conn.execute(
        "INSERT INTO memories (role, content, embedding) VALUES (?, ?, ?)",
        (role, content, json.dumps(emb))
    )
    conn.commit()


def cosine_similarity(v1, v2):
    dot = 0.0
    n1 = 0.0
    n2 = 0.0
    for a, b in zip(v1, v2):
        dot += a * b
        n1 += a * a
        n2 += b * b
    if n1 == 0 or n2 == 0:
        return 0
    return dot / ((n1 ** 0.5) * (n2 ** 0.5))


def search_memories(query_text, limit=5, exclude_recent=10, min_score=0.3):
    if not query_text or not query_text.strip():
        return []
    query_emb = embed_text(query_text)
    if query_emb is None:
        return []
    conn = get_db()
    rows = conn.execute(
        "SELECT id, role, content, embedding, created_at FROM memories "
        "ORDER BY id DESC LIMIT 2000"
    ).fetchall()
    if len(rows) <= exclude_recent:
        return []
    candidates = rows[exclude_recent:]
    scored = []
    for r in candidates:
        try:
            emb = json.loads(r[3])
            score = cosine_similarity(query_emb, emb)
            if score >= min_score:
                scored.append((score, r[1], r[2], r[4]))
        except Exception:
            continue
    scored.sort(reverse=True, key=lambda x: x[0])
    return [{"role": s[1], "content": s[2], "created_at": s[3], "score": s[0]}
            for s in scored[:limit]]


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


def get_int_config(key, default):
    raw = get_config(key, "")
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


TIMING_DEFAULTS = {
    "pre_event_min_1": 30,
    "pre_event_min_2": 10,
    "pre_event_min_3": 2,
    "pre_event_min_4": 0,
    "post_event_min": 30,
    "nudge_window_start_hour": 8,
    "nudge_window_end_hour": 21,
    "nudge_cap_per_task": 5,
    "followup_no_response_hours": 4,
    "message_buffer_seconds": 5,
}


def get_timing(key):
    return get_int_config(key, TIMING_DEFAULTS[key])


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
7. Agendar eventos en Google Calendar, avisar antes (T-30/T-10/T-2/T-0) y hacer follow-up 30 min despues del fin
8. Marcar como completada (complete), descartar sin hacer (kill) o posponer (postpone) tareas existentes
9. Insistir solo (proactivamente) con tareas vencidas: el sistema te muestra cuantos recordatorios lleva cada una y cuantas veces se pospuso
10. Consultar la agenda de un dia (listar_eventos), editar un evento ya agendado (editar_evento) y cancelarlo (cancelar_evento)
11. Buscar en las notas que Christian tiene en su vault de Obsidian (vault_search)

COMPORTAMIENTO PROACTIVO (importante para interpretar el historial):
- Tu envias mensajes solo (sin que Christian escriba) en 3 casos: avisos pre-evento, follow-up post-evento ("¿se dio? ¿que quedo pendiente?") y recordatorios de tareas vencidas.
- En el conversation history puedes ver mensajes tuyos sin un mensaje previo del usuario - esos son proactivos.
- Si tu ultimo mensaje fue un follow-up post-evento y Christian responde, usa intent event_followup_response (no chat, no task).
- Si Christian responde a un recordatorio proactivo de tarea con "lo hago mañana / el viernes / mas tarde", usa intent postpone (no chat).

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
- kill: cuando quiere DESCARTAR una tarea sin haberla hecho (detecta: "olvidalo", "ya no", "matala", "borra eso", "descarta", "cancelala", "quitala"). Marca la tarea como killed sin completarla.
- postpone: cuando POSTERGA una tarea existente para otro dia (detecta: "lo hago mañana", "el viernes lo veo", "mas tarde", "lo dejo para X dia"). Identifica de cual tarea pendiente esta hablando y calcula la nueva fecha. NO uses postpone para tareas nuevas - solo para mover tareas ya en la lista.
- learn: cuando te ENSENA algo sobre el o sus proyectos (detecta: "recuerda que", "mi prioridad es", "ten en cuenta", "aprende que", "esto es importante")
- agendar_evento: cuando quiere AGENDAR algo en su CALENDARIO de Google (detecta: "agendame", "agrega al calendario", "pon una reunion", "tengo una cita", "reserva X dia"). Distinto de reminder: agendar_evento crea evento en Google Calendar; reminder es solo un mensaje.
- listar_eventos: cuando PREGUNTA por su agenda/calendario (detecta: "que tengo hoy", "que reuniones tengo", "que hay manana", "mi agenda del viernes", "que tengo agendado"). Calcula el dia concreto en formato YYYY-MM-DD. Distinto de query: query es sobre tareas/ideas, listar_eventos es sobre eventos del calendario.
- editar_evento: cuando quiere CAMBIAR un evento ya agendado (detecta: "muevelo a las 4", "cambia la reunion al jueves", "pasa la cita a las 10", "reagenda", "que dure 30 min", "renombra el evento"). Identifica el evento por su titulo (search_term) y extrae solo los campos que cambian; deja en null los que no.
- cancelar_evento: cuando quiere CANCELAR o BORRAR un evento del calendario (detecta: "cancela la reunion con Juan", "borra el evento", "elimina la cita"). Distinto de kill: kill es para tareas, cancelar_evento para eventos del calendario.
- event_followup_response: SOLO cuando en la conversacion reciente TU (asistente) hiciste una pregunta del tipo "Tu reunion X termino hace ~30 min. ¿Se dio? ¿Que quedo pendiente?" y el usuario esta respondiendo a esa pregunta especifica. Detecta outcome (si la reunion ocurrio o no) y extrae los pendientes que menciona.
- redactar: cuando quiere AYUDA PARA ESCRIBIR un mensaje a alguien (detecta: "ayudame a escribirle", "como le digo a", "que le respondo a", "redactame", "escribirle a", "mensaje para"). Genera 3 variaciones en SU voz (amigable colombiano informal, directo, calido, nunca corporativo).
- vault_search: cuando pide BUSCAR ALGO en sus notas/vault/obsidian (detecta: "busca en mis notas", "que tengo escrito sobre", "que dije sobre X en obsidian", "revisa mi vault", "mis notas de X"). Extrae el termino a buscar. Distinto de query: query es sobre tareas/ideas guardadas en la DB; vault_search es sobre el texto de las notas markdown del vault.
- chat: conversacion normal, consejo, ayuda para pensar

DATA POR INTENT:
task: {"title":"corto","description":"detalle o null","priority":"alta|media|baja","category":"yave|loslagos|personal|general","due_date":"YYYY-MM-DD o null"}
idea: {"content":"la idea completa","category":"yave|loslagos|personal|general","tags":["tag1"]}
reminder: {"message":"que recordar","remind_at":"YYYY-MM-DD HH:MM"}
query: {"query_type":"pending_tasks|ideas|today|overdue|category","category":"DEBE ser null por DEFAULT. Solo poner yave|loslagos|personal|general SI la pregunta menciona EXPLICITAMENTE ese proyecto. Ejemplo: 'que tareas tengo' -> category null. 'que tareas tengo de yave' -> category yave."}
complete: {"search_term":"texto para buscar la tarea"}
kill: {"search_term":"texto para buscar la tarea"}
postpone: {"search_term":"texto para buscar la tarea","new_due_date":"YYYY-MM-DD"}
learn: {"key":"tema corto","value":"lo que debe recordar"}
agendar_evento: {"title":"corto","start_date":"YYYY-MM-DD","start_time":"HH:MM","duration_minutes":60,"description":"opcional o null","attendees":["email1@x.com","email2@y.com"] o []}
listar_eventos: {"day":"YYYY-MM-DD"}
editar_evento: {"search_term":"texto del titulo del evento a buscar","new_date":"YYYY-MM-DD o null","new_start_time":"HH:MM o null","new_duration_minutes":numero o null,"new_title":"nuevo titulo o null"}
cancelar_evento: {"search_term":"texto del titulo del evento a buscar"}
event_followup_response: {"outcome":"happened|didnt_happen|unknown","pending_tasks":["titulo corto 1","titulo corto 2"]}
redactar: {"destinatario":"para quien es el mensaje","contexto":"que quiere comunicar","variaciones":["v1 mas corta/directa","v2 mas calida","v3 alternativa"]}
vault_search: {"query":"texto a buscar en las notas"}
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
- Si en MEMORIAS RELEVANTES ves algo conectado con lo que esta diciendo ahora (contradiccion, repeticion, contexto previo), mencionalo brevemente.

TONO ADAPTATIVO SEGUN HISTORIAL DE LA TAREA:
- Si una tarea tiene `nudges: N` con N >= 3, o `pospuesta N v` con N >= 2: NO la trates suave. Confronta con los datos ("ya te la recorde 3 veces", "ya la pospusiste 2 veces"). Pide decision concreta: hacerla ahora con un primer paso de 5 min, replantear con fecha y hora reales, o matarla (intent kill).
- "Mañana" no es una respuesta valida si ya dijo "mañana" antes. Pide hora concreta.
- Si Christian dice "lo hago despues / mañana / mas tarde" sobre una tarea existente, eso es postergacion: emite intent postpone, no chat.

CONCIENCIA DE CALENDARIO:
- Si en EVENTOS PROXIMAS 24H hay un evento empezando en menos de 15 min, considera mencionarlo en tu respuesta o moderar la conversacion ("oye, tienes X en 10 min, esto lo vemos despues?").
- No agendes tareas o reminders que pisen un evento existente sin avisar.

INVITADOS EN agendar_evento (campo attendees):
- Si Christian incluye emails en el mensaje ("agenda reunion con juan@x.com y maria@y.com"), ponlos todos en attendees. Google les manda invitacion automatica.
- Si menciona personas por nombre sin email ("agenda reunion con Juan"), busca el email en CONOCIMIENTO PERSONAL DE CHRISTIAN (claves como "email_juan", "correo_juan", etc.). Si lo encuentras, ponlo en attendees. Si NO lo encuentras, igual crea el evento con attendees=[] y en response_text pide el email faltante ("agendado, pero no tengo el email de Juan - pasamelo y lo agrego / la proxima me lo aprendo con 'recuerda que email_juan es ...'").
- Si no menciona a nadie mas, attendees=[].
- Nunca inventes emails.
{{TASKS_BLOCK}}{{EVENTS_BLOCK}}{{IDEAS_BLOCK}}{{MEMORIES_BLOCK}}

FECHA: {{CURRENT_DATE}} ({{DAY_NAME}}) | HORA: {{CURRENT_TIME}}

Para reminders: calcula fecha/hora real. 'manana a las 8' = fecha de manana 08:00. 'en 2 horas' = suma desde hora actual."""


DEFAULT_EMAIL_CRITERIA = """Eres el filtro de correo de Christian. Tu trabajo: decidir si un correo merece interrumpirlo con una notificacion de WhatsApp, o no.

Christian es CEO de dos empresas: Los Lagos (desarrollo inmobiliario) y YAVE (un CRM). Su bandeja recibe MUCHISIMO ruido automatico. Solo lo que de verdad importa debe pasar el filtro.

ES IMPORTANTE (notificar):
- Compras hechas con su tarjeta Bancolombia (debito o credito).
- Correos de Meta/Facebook SOLO si algo sale mal: un anuncio rechazado, un problema de cobro o con la cuenta publicitaria.
- Un correo escrito por una persona real dirigido a el: un lead, cliente, socio o proveedor.
- Algo que pida una respuesta o accion suya, o que tenga una fecha limite.
- Alertas de seguridad reales de sus cuentas (un acceso o cambio que no reconozca).

NO ES IMPORTANTE (ignorar):
- Newsletters, publicidad, promociones, ofertas.
- Recibos y confirmaciones automaticas de rutina.
- Notificaciones de redes sociales o de plataformas.
- Correos de Meta cuando todo va bien: "anuncio aprobado", recibos de pago de publicidad.
- Transferencias bancarias de rutina (salvo que sean por un monto inusualmente alto).

ANTE LA DUDA: marca como importante. Christian prefiere recibir un aviso de mas que perderse algo."""


def build_system_prompt(query_text=None):
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
        today = now.date()
        for t in tasks[:10]:
            meta = []
            if t.get("due_date"):
                meta.append("vence: " + t["due_date"])
                try:
                    d = datetime.strptime(t["due_date"], "%Y-%m-%d").date()
                    days = (today - d).days
                    if days > 0:
                        meta.append("vencida " + str(days) + " d")
                except Exception:
                    pass
            if t.get("nudge_count", 0) > 0:
                meta.append("nudges: " + str(t["nudge_count"]))
            if t.get("postpone_count", 0) > 0:
                meta.append("pospuesta " + str(t["postpone_count"]) + " v")
            suffix = " [" + ", ".join(meta) + "]" if meta else ""
            tasks_block = tasks_block + "- [" + t["priority"] + "] [" + t["category"] + "] " + t["title"] + suffix + "\n"
        if len(tasks) > 10:
            tasks_block = tasks_block + "... y " + str(len(tasks) - 10) + " mas\n"

    ideas = get_recent_ideas(5)
    ideas_block = ""
    if ideas:
        ideas_block = "\n\nIDEAS RECIENTES:\n"
        for i in ideas:
            ideas_block = ideas_block + "- [" + i["category"] + "] " + i["content"] + "\n"

    events_block = ""
    if gcal.is_configured():
        try:
            upcoming = gcal.list_events_next_hours(24)
            if upcoming:
                events_block = "\n\nEVENTOS EN LAS PROXIMAS 24H:\n"
                for e in upcoming[:8]:
                    title = e.get("summary", "(sin titulo)")
                    start = e.get("start", {}).get("dateTime", "")
                    try:
                        sdt = datetime.fromisoformat(start)
                        when = sdt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        when = start
                    events_block = events_block + "- " + when + " - " + title + "\n"
        except Exception as e:
            logger.error("events_block error: %s", e)

    memories_block = ""
    if query_text:
        memories = search_memories(query_text, limit=5)
        if memories:
            memories_block = "\n\nMEMORIAS RELEVANTES (conversaciones pasadas relacionadas a lo que esta diciendo ahora):\n"
            for m in memories:
                role_label = "Christian" if m["role"] == "user" else "Yo"
                date_label = m["created_at"][:10] if m["created_at"] else ""
                content_short = m["content"][:200]
                memories_block = memories_block + "- [" + date_label + "] " + role_label + ": " + content_short + "\n"

    body = get_config("system_prompt", DEFAULT_PROMPT_BODY)
    return (body
        .replace("{{CONTEXT_BLOCK}}", context_block)
        .replace("{{TASKS_BLOCK}}", tasks_block)
        .replace("{{IDEAS_BLOCK}}", ideas_block)
        .replace("{{EVENTS_BLOCK}}", events_block)
        .replace("{{MEMORIES_BLOCK}}", memories_block)
        .replace("{{CURRENT_DATE}}", current_date)
        .replace("{{DAY_NAME}}", day_name)
        .replace("{{CURRENT_TIME}}", current_time)
    )


async def interpret_message(text):
    prompt = build_system_prompt(query_text=text)
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
        "INSERT INTO tasks (title,description,priority,category,due_date,source_event_id) VALUES (?,?,?,?,?,?)",
        (data.get("title", "Sin titulo"), data.get("description"),
         data.get("priority", "media"), data.get("category", "general"),
         data.get("due_date"), data.get("source_event_id"))
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
    cols = ("id,title,description,priority,category,due_date,status,created_at,completed_at,"
            "nudge_count,last_nudged_at,postpone_count")
    if category and category != "null":
        rows = conn.execute(
            "SELECT " + cols + " FROM tasks WHERE status='pendiente' AND category=? "
            "ORDER BY CASE priority WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END, due_date",
            (category,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT " + cols + " FROM tasks WHERE status='pendiente' "
            "ORDER BY CASE priority WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END, due_date"
        ).fetchall()
    result = []
    for r in rows:
        result.append({"id": r[0], "title": r[1], "description": r[2], "priority": r[3],
                        "category": r[4], "due_date": r[5], "status": r[6],
                        "created_at": r[7], "completed_at": r[8],
                        "nudge_count": r[9] or 0, "last_nudged_at": r[10],
                        "postpone_count": r[11] or 0})
    return result


def get_overdue_tasks():
    today = datetime.now(tz).strftime("%Y-%m-%d")
    conn = get_db()
    rows = conn.execute(
        "SELECT id,title,description,priority,category,due_date,status,created_at,completed_at,"
        "nudge_count,last_nudged_at,postpone_count "
        "FROM tasks WHERE status='pendiente' AND due_date<? AND due_date IS NOT NULL",
        (today,)
    ).fetchall()
    result = []
    for r in rows:
        result.append({"id": r[0], "title": r[1], "description": r[2], "priority": r[3],
                        "category": r[4], "due_date": r[5], "status": r[6],
                        "created_at": r[7], "completed_at": r[8],
                        "nudge_count": r[9] or 0, "last_nudged_at": r[10],
                        "postpone_count": r[11] or 0})
    return result


def get_tasks_due_on(date_str):
    conn = get_db()
    rows = conn.execute(
        "SELECT id,title,description,priority,category,due_date "
        "FROM tasks WHERE status='pendiente' AND due_date=? "
        "ORDER BY CASE priority WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END",
        (date_str,)
    ).fetchall()
    return [{"id": r[0], "title": r[1], "description": r[2], "priority": r[3],
             "category": r[4], "due_date": r[5]} for r in rows]


def get_tasks_to_nudge(cap):
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    now_iso = datetime.now(tz).isoformat()
    conn = get_db()
    rows = conn.execute(
        "SELECT id,title,description,priority,category,due_date,created_at,"
        "nudge_count,last_nudged_at,postpone_count "
        "FROM tasks WHERE status='pendiente' "
        "AND due_date IS NOT NULL AND due_date < ? "
        "AND (last_nudged_at IS NULL OR last_nudged_at < ?) "
        "AND COALESCE(nudge_count, 0) < ? "
        "AND (snoozed_until IS NULL OR snoozed_until < ?) "
        "ORDER BY CASE priority WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END, due_date "
        "LIMIT 10",
        (today_str, today_start, cap, now_iso)
    ).fetchall()
    result = []
    for r in rows:
        result.append({"id": r[0], "title": r[1], "description": r[2], "priority": r[3],
                       "category": r[4], "due_date": r[5], "created_at": r[6],
                       "nudge_count": r[7] or 0, "last_nudged_at": r[8],
                       "postpone_count": r[9] or 0})
    return result


def increment_nudge_count(task_id):
    conn = get_db()
    now_iso = datetime.now(tz).isoformat()
    conn.execute(
        "UPDATE tasks SET nudge_count = COALESCE(nudge_count, 0) + 1, last_nudged_at=? WHERE id=?",
        (now_iso, task_id)
    )
    conn.commit()


def kill_task(search_term):
    conn = get_db()
    tasks = conn.execute(
        "SELECT id,title FROM tasks WHERE status='pendiente' AND LOWER(title) LIKE ?",
        ("%" + search_term.lower() + "%",)
    ).fetchall()
    if len(tasks) == 1:
        conn.execute("UPDATE tasks SET status='killed' WHERE id=?", (tasks[0][0],))
        conn.commit()
        return tasks[0][1]
    elif len(tasks) > 1:
        return "MULTIPLE:" + ", ".join(t[1] for t in tasks)
    return None


def postpone_task(search_term, new_due_date):
    conn = get_db()
    tasks = conn.execute(
        "SELECT id,title FROM tasks WHERE status='pendiente' AND LOWER(title) LIKE ?",
        ("%" + search_term.lower() + "%",)
    ).fetchall()
    if len(tasks) == 1:
        conn.execute(
            "UPDATE tasks SET due_date=?, postpone_count = COALESCE(postpone_count, 0) + 1 WHERE id=?",
            (new_due_date, tasks[0][0])
        )
        conn.commit()
        return tasks[0][1]
    elif len(tasks) > 1:
        return "MULTIPLE:" + ", ".join(t[1] for t in tasks)
    return None


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


def add_event_followup(event_id, event_title, event_end_at_iso, scheduled_at_iso):
    conn = get_db()
    conn.execute(
        "INSERT INTO event_followups (event_id, event_title, event_end_at, scheduled_at) "
        "VALUES (?, ?, ?, ?)",
        (event_id, event_title, event_end_at_iso, scheduled_at_iso)
    )
    conn.commit()


def get_due_event_followups():
    conn = get_db()
    now_iso = datetime.now(tz).isoformat()
    rows = conn.execute(
        "SELECT id, event_id, event_title, event_end_at, scheduled_at "
        "FROM event_followups WHERE status='pending' AND scheduled_at<=?",
        (now_iso,)
    ).fetchall()
    return [
        {"id": r[0], "event_id": r[1], "event_title": r[2], "event_end_at": r[3], "scheduled_at": r[4]}
        for r in rows
    ]


def mark_event_followup_sent(followup_id):
    conn = get_db()
    now_iso = datetime.now(tz).isoformat()
    conn.execute(
        "UPDATE event_followups SET status='sent', sent_at=? WHERE id=?",
        (now_iso, followup_id)
    )
    conn.commit()


def get_active_event_followup():
    conn = get_db()
    row = conn.execute(
        "SELECT id, event_id, event_title FROM event_followups "
        "WHERE status='sent' AND responded_at IS NULL "
        "ORDER BY sent_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "event_id": row[1], "event_title": row[2]}


def mark_event_followup_responded(followup_id, outcome):
    conn = get_db()
    now_iso = datetime.now(tz).isoformat()
    conn.execute(
        "UPDATE event_followups SET status='responded', responded_at=?, outcome=? WHERE id=?",
        (now_iso, outcome, followup_id)
    )
    conn.commit()


def expire_stale_event_followups(hours=4):
    conn = get_db()
    cutoff_iso = (datetime.now(tz) - timedelta(hours=hours)).isoformat()
    conn.execute(
        "UPDATE event_followups SET status='no_response' "
        "WHERE status='sent' AND responded_at IS NULL AND sent_at<?",
        (cutoff_iso,)
    )
    conn.commit()


def cancel_event_followups(event_id):
    conn = get_db()
    conn.execute(
        "UPDATE event_followups SET status='cancelled' "
        "WHERE event_id=? AND status IN ('pending', 'sent')",
        (event_id,)
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


def format_vault_results(query, results):
    if not results:
        return "\U0001f4d3 No encontre nada en tu vault con: " + query
    lines = ["\U0001f4d3 *Tu vault - " + query + "*"]
    for r in results:
        path = r.get("path", "").rsplit(".", 1)[0]
        lines.append("\n• " + path)
        snippet = r.get("snippet", "")
        if snippet:
            lines.append("  _" + snippet.replace("\n", " ") + "_")
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


def _build_event_patch(event, data):
    """Calcula (summary, start_iso, end_iso) para gcal.update_event.

    Conserva la duracion del evento salvo que se pida una nueva. Devuelve None
    si no hay nada que cambiar o si la fecha/hora no se puede aplicar.
    """
    new_title = (data.get("new_title") or "").strip() or None
    new_date = (data.get("new_date") or "").strip()
    new_time = (data.get("new_start_time") or "").strip()
    new_dur = data.get("new_duration_minutes")

    start_str = event.get("start", {}).get("dateTime", "")
    end_str = event.get("end", {}).get("dateTime", "")

    # Evento de dia completo: solo se le puede cambiar el titulo.
    if not start_str:
        return (new_title, None, None) if new_title else None

    if not (new_date or new_time or new_dur):
        return (new_title, None, None) if new_title else None

    try:
        cur_start = datetime.fromisoformat(start_str)
        cur_end = datetime.fromisoformat(end_str) if end_str else cur_start + timedelta(hours=1)
    except Exception:
        return None

    target_date = cur_start.date()
    if new_date:
        try:
            target_date = datetime.strptime(new_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    target_time = cur_start.timetz()
    if new_time:
        try:
            target_time = datetime.strptime(new_time, "%H:%M").time().replace(tzinfo=cur_start.tzinfo)
        except ValueError:
            pass

    new_start = datetime.combine(target_date, target_time)
    if new_start.tzinfo is None:
        new_start = new_start.replace(tzinfo=tz)

    duration = cur_end - cur_start
    if new_dur:
        try:
            duration = timedelta(minutes=int(new_dur))
        except (ValueError, TypeError):
            pass

    return (new_title, new_start.isoformat(), (new_start + duration).isoformat())


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
            "*Calendario:*\n"
            "- _Agenda reunion con Juan el jueves 10am_\n"
            "- _Que tengo manana_\n"
            "- _Muevela a las 4pm_ / _Cancela la reunion con Juan_\n\n"
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
        await obsidian.capture_task(tid, data)

    elif intent == "idea":
        iid = add_idea(data)
        msg = "\U0001f4a1 Idea #" + str(iid) + " guardada\n_" + data.get("content", "") + "_"
        if response_text and response_text != data.get("content", ""):
            msg = msg + "\n\n" + response_text
        await send_whatsapp(phone, msg)
        await obsidian.capture_idea(iid, data)

    elif intent == "reminder":
        rid = add_reminder(data)
        msg = "\u23f0 Recordatorio #" + str(rid) + "\n_" + data.get("message", "") + "_\n\U0001f550 " + data.get("remind_at", "")
        if response_text:
            msg = msg + "\n\n" + response_text
        await send_whatsapp(phone, msg)
        await obsidian.capture_reminder(rid, data)

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
            await obsidian.mark_task_completed(found)
        else:
            await send_whatsapp(phone, "\U0001f50d No encontre esa tarea. Escribe *pendientes* para ver la lista.")

    elif intent == "kill":
        search = data.get("search_term", "")
        found = kill_task(search)
        if found and found.startswith("MULTIPLE:"):
            await send_whatsapp(phone, "\U0001f914 Varias coinciden:\n" + found[9:] + "\n\nSe mas especifico.")
        elif found:
            msg = "\U0001f5d1 *Descartada:* " + found
            if response_text:
                msg = msg + "\n\n" + response_text
            await send_whatsapp(phone, msg)
            await obsidian.mark_task_killed(found)
        else:
            await send_whatsapp(phone, "\U0001f50d No encontre esa tarea para descartar.")

    elif intent == "postpone":
        search = data.get("search_term", "")
        new_date = data.get("new_due_date", "")
        if not new_date:
            await send_whatsapp(phone, "¿Para que fecha la posponemos? Dame el dia concreto.")
        else:
            found = postpone_task(search, new_date)
            if found and found.startswith("MULTIPLE:"):
                await send_whatsapp(phone, "\U0001f914 Varias coinciden:\n" + found[9:] + "\n\nSe mas especifico.")
            elif found:
                msg = "⏭ Pospuesta: *" + found + "* -> " + new_date
                if response_text:
                    msg = msg + "\n\n" + response_text
                await send_whatsapp(phone, msg)
            else:
                await send_whatsapp(phone, "\U0001f50d No encontre esa tarea para posponer.")

    elif intent == "learn":
        key = data.get("key", "")
        value = data.get("value", "")
        if key and value:
            set_context(key, value)
            await send_whatsapp(phone, "\U0001f9e0 Listo, me lo guarde.\n\n" + response_text)
            await obsidian.capture_context(key, value)
        else:
            await send_whatsapp(phone, response_text)

    elif intent == "redactar":
        variaciones = data.get("variaciones", [])
        destinatario = data.get("destinatario", "")
        if variaciones:
            header = "✍️ *Opciones"
            if destinatario:
                header = header + " para " + destinatario
            header = header + ":*"
            msg = header
            for i, v in enumerate(variaciones, 1):
                msg = msg + "\n\n*" + str(i) + ".* " + v
            if response_text and response_text not in variaciones:
                msg = msg + "\n\n_" + response_text + "_"
            await send_whatsapp(phone, msg)
        else:
            await send_whatsapp(phone, response_text or "No pude generar las variaciones, reformula?")

    elif intent == "agendar_evento":
        if not gcal.is_configured():
            await send_whatsapp(phone, "Google Calendar no esta configurado todavia.")
        else:
            title = data.get("title", "")
            start_date = data.get("start_date", "")
            start_time = data.get("start_time", "")
            duration_min = data.get("duration_minutes") or 60
            description = data.get("description")
            attendees_raw = data.get("attendees") or []
            attendees = [str(a).strip() for a in attendees_raw if str(a or "").strip() and "@" in str(a)]
            if not title or not start_date or not start_time:
                await send_whatsapp(phone, "No entendi bien la fecha/hora del evento. Reformulalo?")
            else:
                try:
                    start_dt = datetime.strptime(start_date + " " + start_time, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                    end_dt = start_dt + timedelta(minutes=int(duration_min))
                    event = gcal.create_event(
                        title,
                        start_dt.isoformat(),
                        end_dt.isoformat(),
                        description,
                        attendees=attendees,
                    )
                    if event:
                        evt_id = event.get("id", "")
                        if evt_id:
                            followup_at = end_dt + timedelta(minutes=get_timing("post_event_min"))
                            add_event_followup(evt_id, title, end_dt.isoformat(), followup_at.isoformat())
                        msg = gcal.format_event_for_creation(event)
                        if response_text:
                            msg = msg + "\n\n" + response_text
                        await send_whatsapp(phone, msg)
                    else:
                        await send_whatsapp(phone, "Hubo un error creando el evento. Intenta de nuevo.")
                except Exception as e:
                    logger.error("Error procesando agendar_evento: %s", e)
                    await send_whatsapp(phone, "Hubo un error con la fecha/hora. Intenta de nuevo.")

    elif intent == "event_followup_response":
        active = get_active_event_followup()
        if not active:
            await send_whatsapp(phone, response_text or "Anotado.")
        else:
            outcome = data.get("outcome", "unknown")
            mark_event_followup_responded(active["id"], outcome)
            pending = data.get("pending_tasks") or []
            created_ids = []
            for title in pending:
                if not title or not str(title).strip():
                    continue
                tid = add_task({
                    "title": str(title).strip(),
                    "priority": "media",
                    "category": "general",
                    "source_event_id": active["event_id"],
                })
                created_ids.append(tid)
            if created_ids:
                msg = "✅ Anotado. " + str(len(created_ids)) + " pendiente(s) guardado(s) de *" + (active.get("event_title") or "la reunion") + "*."
            elif outcome == "didnt_happen":
                msg = "\U0001f44c Marcada como no realizada."
            else:
                msg = "\U0001f44d Anotado."
            if response_text and response_text not in msg:
                msg = msg + "\n\n" + response_text
            await send_whatsapp(phone, msg)

    elif intent == "listar_eventos":
        if not gcal.is_configured():
            await send_whatsapp(phone, "Google Calendar no esta configurado todavia.")
        else:
            day = (data.get("day") or "").strip() or datetime.now(tz).strftime("%Y-%m-%d")
            try:
                d = datetime.strptime(day, "%Y-%m-%d")
                day_label = _DIAS_ES[d.weekday()] + " " + d.strftime("%d/%m")
            except ValueError:
                day_label = day
            events = gcal.list_events_on_date(day)
            if not events:
                msg = "\U0001f4c5 " + day_label + ": no tienes nada agendado."
                if response_text:
                    msg = msg + "\n\n" + response_text
                await send_whatsapp(phone, msg)
            else:
                lines = ["\U0001f4c5 *Agenda " + day_label + "* (" + str(len(events)) + ")", ""]
                for e in events:
                    lines.append(gcal.format_event_for_daily_preview(e))
                msg = "\n".join(lines)
                if response_text:
                    msg = msg + "\n\n" + response_text
                await send_whatsapp(phone, msg)

    elif intent == "editar_evento":
        if not gcal.is_configured():
            await send_whatsapp(phone, "Google Calendar no esta configurado todavia.")
        else:
            search = (data.get("search_term") or "").strip()
            matches = gcal.find_events(search) if search else []
            if not matches:
                await send_whatsapp(phone, "\U0001f50d No encontre ese evento en tu calendario.")
            elif len(matches) > 1:
                lines = ["\U0001f914 Varios eventos coinciden:"]
                for e in matches[:6]:
                    lines.append("• " + gcal.format_event_oneline(e))
                lines.append("\nSe mas especifico.")
                await send_whatsapp(phone, "\n".join(lines))
            else:
                event = matches[0]
                patch = _build_event_patch(event, data)
                if not patch:
                    await send_whatsapp(phone, "Dime que quieres cambiarle al evento: hora, fecha, duracion o titulo.")
                else:
                    summary, start_iso, end_iso = patch
                    updated = gcal.update_event(event["id"], summary=summary, start_iso=start_iso, end_iso=end_iso)
                    if updated:
                        msg = "✏️ Evento actualizado:\n" + gcal.format_event_for_creation(updated)
                        if response_text:
                            msg = msg + "\n\n" + response_text
                        await send_whatsapp(phone, msg)
                    else:
                        await send_whatsapp(phone, "Hubo un error actualizando el evento. Intenta de nuevo.")

    elif intent == "cancelar_evento":
        if not gcal.is_configured():
            await send_whatsapp(phone, "Google Calendar no esta configurado todavia.")
        else:
            search = (data.get("search_term") or "").strip()
            matches = gcal.find_events(search) if search else []
            if not matches:
                await send_whatsapp(phone, "\U0001f50d No encontre ese evento en tu calendario.")
            elif len(matches) > 1:
                lines = ["\U0001f914 Varios eventos coinciden:"]
                for e in matches[:6]:
                    lines.append("• " + gcal.format_event_oneline(e))
                lines.append("\nSe mas especifico.")
                await send_whatsapp(phone, "\n".join(lines))
            else:
                event = matches[0]
                if gcal.delete_event(event["id"]):
                    cancel_event_followups(event["id"])
                    msg = "\U0001f5d1 Evento cancelado: *" + event.get("summary", "(sin titulo)") + "*"
                    if response_text:
                        msg = msg + "\n\n" + response_text
                    await send_whatsapp(phone, msg)
                else:
                    await send_whatsapp(phone, "Hubo un error cancelando el evento. Intenta de nuevo.")

    elif intent == "vault_search":
        if not obsidian.is_configured():
            await send_whatsapp(phone, "Tu vault de Obsidian no esta conectado todavia.")
        else:
            query = (data.get("query") or "").strip()
            if not query:
                await send_whatsapp(phone, "¿Qué busco en tu vault?")
            else:
                results = await obsidian.search_notes(query, limit=5)
                await send_whatsapp(phone, format_vault_results(query, results))

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

    if gcal.is_configured():
        try:
            today_events = gcal.list_events_on_date(datetime.now(tz).strftime("%Y-%m-%d"))
        except Exception as e:
            logger.error("morning_summary events error: %s", e)
            today_events = []
        if today_events:
            msg = msg + "\n\n\U0001f4c5 *AGENDA HOY:*"
            for e in today_events:
                msg = msg + "\n" + gcal.format_event_for_daily_preview(e)

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


_DIAS_ES = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]

_PRIO_EMOJI = {"alta": "\U0001f534", "media": "\U0001f7e1", "baja": "⚪"}


def _format_task_line(t):
    prio = _PRIO_EMOJI.get(t.get("priority", "media"), "⚪")
    cat = C_EMOJI.get(t.get("category", "general"), "\U0001f4cc")
    return "• " + prio + " " + cat + " " + t.get("title", "")


async def tomorrow_preview():
    if not MY_PHONE_NUMBER:
        return
    t = datetime.now(tz) + timedelta(days=1)
    tomorrow_label = _DIAS_ES[t.weekday()] + " " + t.strftime("%d/%m")
    tomorrow_iso = t.strftime("%Y-%m-%d")

    events = gcal.list_events_tomorrow() if gcal.is_configured() else []
    tasks_tomorrow = get_tasks_due_on(tomorrow_iso)
    overdue = get_overdue_tasks()

    if not events and not tasks_tomorrow and not overdue:
        msg = (
            "\U0001f306 *Preview mañana* (" + tomorrow_label + ")\n\n"
            "Sin reuniones ni tareas. Dia libre para enfocarte."
        )
        await send_whatsapp(MY_PHONE_NUMBER, msg)
        return

    sections = ["\U0001f306 *Preview mañana* (" + tomorrow_label + ")"]

    sections.append("")
    if events:
        sections.append("\U0001f4c5 *Reuniones* (" + str(len(events)) + ")")
        for e in events:
            sections.append(gcal.format_event_for_daily_preview(e))
    elif gcal.is_configured():
        sections.append("\U0001f4c5 *Reuniones:* ninguna agendada")

    if tasks_tomorrow:
        sections.append("")
        sections.append("\U0001f4cb *Tareas para mañana* (" + str(len(tasks_tomorrow)) + ")")
        for tk in tasks_tomorrow:
            sections.append(_format_task_line(tk))

    if overdue:
        sections.append("")
        sections.append("⚠️ *Vencidas arrastradas* (" + str(len(overdue)) + ")")
        for tk in overdue[:8]:
            line = _format_task_line(tk)
            if tk.get("due_date"):
                line = line + " _(" + tk["due_date"] + ")_"
            sections.append(line)
        if len(overdue) > 8:
            sections.append("…y " + str(len(overdue) - 8) + " mas")

    await send_whatsapp(MY_PHONE_NUMBER, "\n".join(sections))


def get_weekly_stats():
    now = datetime.now(tz)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    today_str = now.strftime("%Y-%m-%d")
    conn = get_db()
    created = conn.execute(
        "SELECT category, COUNT(*) FROM tasks WHERE created_at >= ? GROUP BY category",
        (week_ago,)
    ).fetchall()
    completed = conn.execute(
        "SELECT category, COUNT(*) FROM tasks WHERE completed_at >= ? GROUP BY category",
        (week_ago,)
    ).fetchall()
    ideas_week = conn.execute(
        "SELECT category, COUNT(*) FROM ideas WHERE created_at >= ? GROUP BY category",
        (week_ago,)
    ).fetchall()
    pending = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='pendiente'"
    ).fetchone()[0]
    pending_high = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='pendiente' AND priority='alta'"
    ).fetchone()[0]
    overdue = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='pendiente' AND due_date < ? AND due_date IS NOT NULL",
        (today_str,)
    ).fetchone()[0]
    return {
        "created_by_cat": {r[0]: r[1] for r in created},
        "completed_by_cat": {r[0]: r[1] for r in completed},
        "ideas_by_cat": {r[0]: r[1] for r in ideas_week},
        "total_created": sum(r[1] for r in created),
        "total_completed": sum(r[1] for r in completed),
        "total_ideas": sum(r[1] for r in ideas_week),
        "pending": pending,
        "pending_high": pending_high,
        "overdue": overdue,
    }


async def weekly_review():
    if not MY_PHONE_NUMBER:
        return
    stats = get_weekly_stats()
    ctx = get_all_context()
    priority = ctx.get("prioridad_semana") or ctx.get("prioridad") or ""

    def fmt_cat_dict(d):
        if not d:
            return "ninguna"
        return ", ".join(k + ":" + str(v) for k, v in d.items())

    stats_text = (
        "Tareas creadas: " + str(stats["total_created"]) + " (" + fmt_cat_dict(stats["created_by_cat"]) + ")\n"
        "Tareas completadas: " + str(stats["total_completed"]) + " (" + fmt_cat_dict(stats["completed_by_cat"]) + ")\n"
        "Ideas guardadas: " + str(stats["total_ideas"]) + " (" + fmt_cat_dict(stats["ideas_by_cat"]) + ")\n"
        "Pendientes totales: " + str(stats["pending"]) + " (" + str(stats["pending_high"]) + " alta prioridad)\n"
        "Vencidas: " + str(stats["overdue"]) + "\n"
    )
    if priority:
        stats_text = stats_text + "\nPrioridad declarada de Christian: " + priority

    coach_prompt = (
        "Eres el coach de Christian, no su asistente. Acabas de revisar su semana. "
        "Estilo: directo, sin lambisconear, espanol colombiano informal. "
        "Maximo 8 lineas en total. Nada de emojis (solo el del titulo que pone el sistema). "
        "Si hay dispersion entre lo que declaro como prioridad y donde realmente puso energia, dilo claro. "
        "Si la categoria con mas tareas creadas no coincide con la prioridad, mencionalo. "
        "Si completo poco vs lo que creo, mencionalo (ratio creadas:completadas). "
        "Si dejo cosas vencer, sin drama pero menciona. "
        "Si lo hizo bien, reconoce sin exagerar - 1 linea. "
        "Termina con UNA pregunta puntual o UNA sugerencia concreta para la proxima semana. "
        "No repitas los numeros, ya los va a ver arriba. Habla del patron, no de la data.\n\n"
        "Datos de la semana:\n" + stats_text
    )

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-5.4-mini",
            max_completion_tokens=500,
            temperature=0.7,
            messages=[{"role": "system", "content": coach_prompt}]
        )
        analysis = resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error("Weekly review error: %s", e)
        analysis = "No pude generar el analisis esta vez."

    msg = (
        "\U0001f4ca *REVIEW SEMANAL*\n\n"
        + stats_text
        + "\n---\n\n"
        + analysis
    )
    await send_whatsapp(MY_PHONE_NUMBER, msg)


async def check_calendar_events():
    if not gcal.is_configured() or not MY_PHONE_NUMBER:
        return
    targets = [
        get_timing("pre_event_min_1"),
        get_timing("pre_event_min_2"),
        get_timing("pre_event_min_3"),
        get_timing("pre_event_min_4"),
    ]
    max_target = max(targets) if targets else 30
    events = gcal.list_upcoming_events(minutes_ahead=max_target + 5)
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
        for target in targets:
            notif_type = "T-" + str(target)
            if abs(delta_min - target) <= 1.5 and not is_calendar_notification_sent(event_id, notif_type):
                await send_whatsapp(MY_PHONE_NUMBER, gcal.format_event_for_reminder(event, notif_type))
                mark_calendar_notification_sent(event_id, notif_type)


async def check_pending_followups():
    if not MY_PHONE_NUMBER:
        return
    expire_stale_event_followups(hours=get_timing("followup_no_response_hours"))
    due = get_due_event_followups()
    for f in due:
        title = f.get("event_title") or "(sin titulo)"
        post_min = get_timing("post_event_min")
        msg = (
            "\U0001f50d Tu reunion *" + title + "* termino hace ~" + str(post_min) + " min.\n"
            "¿Se dio? ¿Que quedo pendiente?"
        )
        await send_whatsapp(MY_PHONE_NUMBER, msg)
        save_conversation("assistant", msg)
        mark_event_followup_sent(f["id"])


def generate_nudge_message(task, cap):
    today = datetime.now(tz).date()
    days_overdue = 0
    if task.get("due_date"):
        try:
            d = datetime.strptime(task["due_date"], "%Y-%m-%d").date()
            days_overdue = max(0, (today - d).days)
        except Exception:
            pass
    nudge_count = task.get("nudge_count", 0)
    postpone_count = task.get("postpone_count", 0)
    is_final = nudge_count + 1 >= cap

    sys = (
        "Eres el asistente personal de Christian. Vas a generar UN mensaje proactivo "
        "para recordarle UNA tarea pendiente. Una sola linea, dos como mucho. "
        "Espanol colombiano informal. Sin saludos. Sin emojis salvo si suma. "
        "Tono basado en datos:\n"
        "- Si nudges previos == 0 y postergaciones == 0: recordatorio amable.\n"
        "- Si nudges previos >= 2 o postergaciones >= 1: confronta con los datos. Mencionalos.\n"
        "- Si es el ULTIMO recordatorio (cap alcanzado): forzar decision. Pide explicito: "
        "hacerla ahora con un primer paso de 5 min, replantear con fecha y hora concretas, o "
        "matarla. 'Mañana' NO es respuesta valida.\n"
        "Responde SOLO JSON valido sin markdown: {\"message\":\"...\"}"
    )
    user = (
        "Tarea: " + task.get("title", "") + "\n"
        "Prioridad: " + task.get("priority", "media") + "\n"
        "Categoria: " + task.get("category", "general") + "\n"
        "Dias vencida: " + str(days_overdue) + "\n"
        "Nudges previos: " + str(nudge_count) + "\n"
        "Postergaciones previas: " + str(postpone_count) + "\n"
        "Es ultimo recordatorio (forzar decision): " + ("si" if is_final else "no")
    )
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-5.4-mini",
            max_completion_tokens=200,
            temperature=0.7,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
        )
        raw = resp.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        msg = parsed.get("message", "").strip()
        if msg:
            return msg
    except Exception as e:
        logger.error("generate_nudge_message error: %s", e)
    if is_final:
        return ("Sigue pendiente: *" + task.get("title", "") + "*. Ya van " + str(nudge_count) +
                " recordatorios. Decision: la haces ahora con un primer paso de 5 min, "
                "le pones fecha real, o la matas. 'Mañana' no vale otra vez.")
    return ("Recordatorio: *" + task.get("title", "") + "*" +
            (" - vencida " + str(days_overdue) + " d" if days_overdue > 0 else ""))


async def check_overdue_nudges():
    if not MY_PHONE_NUMBER:
        return
    now = datetime.now(tz)
    start_h = get_timing("nudge_window_start_hour")
    end_h = get_timing("nudge_window_end_hour")
    if not (start_h <= now.hour < end_h):
        return
    if gcal.is_configured() and gcal.is_event_active_now():
        return
    cap = get_timing("nudge_cap_per_task")
    tasks = get_tasks_to_nudge(cap)
    for t in tasks:
        msg = generate_nudge_message(t, cap)
        await send_whatsapp(MY_PHONE_NUMBER, msg)
        save_conversation("assistant", msg)
        increment_nudge_count(t["id"])


def classify_email(email):
    """Clasifica un correo como importante o no, segun el criterio editable.
    Devuelve {"important": bool, "reason": str}, o None si la clasificacion
    falla (para reintentarla en el proximo ciclo en vez de descartarla)."""
    criterio = get_config("email_importance_criteria", DEFAULT_EMAIL_CRITERIA)
    sys_prompt = (
        criterio + "\n\n"
        "Responde SOLO con JSON valido, sin markdown ni backticks:\n"
        '{"important": true o false, "reason": "por que, en espanol, max 12 palabras"}'
    )
    user_msg = (
        "De: " + (email.get("from_name") or "") + " <" + (email.get("from_email") or "") + ">\n"
        "Asunto: " + (email.get("subject") or "") + "\n"
        "Resumen: " + (email.get("snippet") or "")
    )
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-5.4-mini",
            max_completion_tokens=150,
            temperature=0.7,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_msg},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        return {
            "important": bool(parsed.get("important", False)),
            "reason": str(parsed.get("reason", "")).strip(),
        }
    except Exception as e:
        logger.error("classify_email error: %s", e)
        return None


def get_processed_email_ids(ids):
    """De una lista de message_ids, devuelve el set de los que ya estan en la DB."""
    if not ids:
        return set()
    conn = get_db()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT message_id FROM processed_emails WHERE message_id IN (" + placeholders + ")",
        tuple(ids),
    ).fetchall()
    return {r[0] for r in rows}


def processed_emails_count():
    conn = get_db()
    return conn.execute("SELECT COUNT(*) FROM processed_emails").fetchone()[0]


def save_processed_email(email, important, reason, notified):
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO processed_emails "
        "(message_id, from_email, subject, important, reason, notified) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (email.get("id"), email.get("from_email"), email.get("subject"),
         1 if important else 0, reason, 1 if notified else 0),
    )
    conn.commit()


def format_email_alert(email, reason):
    sender = email.get("from_name") or email.get("from_email") or "?"
    msg = "\U0001f4e7 *Correo importante*\n\n"
    msg = msg + "De: *" + sender + "*\n"
    msg = msg + "Asunto: " + (email.get("subject") or "(sin asunto)")
    if reason:
        msg = msg + "\n\n_" + reason + "_"
    return msg


async def check_new_emails():
    if not gmail.is_configured() or not MY_PHONE_NUMBER:
        return
    service = gmail.get_gmail_service()
    if not service:
        return
    ids = gmail.list_message_ids(service, "in:inbox newer_than:1d", 40)
    if not ids:
        return
    already = get_processed_email_ids(ids)
    new_ids = [i for i in ids if i not in already]
    if not new_ids:
        return

    # Primera corrida: sembrar el inbox actual sin notificar, para no
    # reenviar correos viejos. Solo se avisa de los que lleguen despues.
    if processed_emails_count() == 0:
        for mid in new_ids:
            save_processed_email({"id": mid}, important=False, reason="(seed)", notified=False)
        logger.info("check_new_emails: %d correos sembrados (primera corrida)", len(new_ids))
        return

    for mid in new_ids:
        email = gmail.get_email(service, mid)
        if not email:
            continue
        result = classify_email(email)
        if result is None:
            continue  # error de clasificacion: se reintenta en el proximo ciclo
        important = result["important"]
        reason = result["reason"]
        notified = False
        if important:
            await send_whatsapp(MY_PHONE_NUMBER, format_email_alert(email, reason))
            notified = True
        save_processed_email(email, important, reason, notified)


app = FastAPI(title="Asistente Personal WhatsApp v3")


@app.on_event("startup")
async def startup():
    init_db()
    migrate_schema()
    scheduler.add_job(check_reminders, IntervalTrigger(minutes=1), id="reminders")
    scheduler.add_job(morning_summary, CronTrigger(hour=7, minute=0), id="morning")
    scheduler.add_job(tomorrow_preview, CronTrigger(hour=19, minute=0), id="tomorrow_preview")
    scheduler.add_job(evening_review, CronTrigger(hour=21, minute=0), id="evening")
    scheduler.add_job(check_overdue_nudges, IntervalTrigger(hours=1), id="nudges")
    scheduler.add_job(weekly_review, CronTrigger(day_of_week="sun", hour=20, minute=0), id="weekly")
    if gcal.is_configured():
        scheduler.add_job(check_calendar_events, IntervalTrigger(minutes=1), id="gcal")
        scheduler.add_job(check_pending_followups, IntervalTrigger(minutes=5), id="followups")
        logger.info("Google Calendar: configurado")
    else:
        logger.warning("Google Calendar: NO configurado (faltan env vars GOOGLE_*)")
    if gmail.is_configured():
        scheduler.add_job(check_new_emails, IntervalTrigger(minutes=2), id="gmail")
        logger.info("Gmail: configurado")
    else:
        logger.warning("Gmail: NO configurado (falta el scope de Gmail en GOOGLE_REFRESH_TOKEN)")
    if obsidian.is_configured():
        logger.info("Obsidian vault: configurado (%s)", obsidian.VAULT_REPO)
    else:
        logger.warning("Obsidian vault: NO configurado (faltan env vars GITHUB_TOKEN / VAULT_REPO)")
    scheduler.start()
    logger.info("Asistente Personal v3 iniciado - Turso DB")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()


QUICK_COMMANDS = {
    "resumen", "como voy", "status",
    "pendientes", "tareas", "mis tareas",
    "ideas", "mis ideas",
    "ayuda",
}

_message_buffers: dict[str, list[str]] = {}
_buffer_tasks: dict[str, asyncio.Task] = {}
_buffer_lock = asyncio.Lock()


async def _flush_buffer(phone: str, wait_seconds: int):
    try:
        await asyncio.sleep(max(1, wait_seconds))
    except asyncio.CancelledError:
        return
    async with _buffer_lock:
        messages = _message_buffers.pop(phone, [])
        _buffer_tasks.pop(phone, None)
    if not messages:
        return
    combined = "\n".join(messages)
    try:
        await process_message(phone, combined)
    except Exception as e:
        logger.error("Error procesando buffer: %s", e)


async def buffer_and_process(phone: str, text: str):
    # Comandos rápidos: drenar cualquier buffer pendiente y procesar de inmediato.
    if text.strip().lower() in QUICK_COMMANDS:
        async with _buffer_lock:
            existing = _buffer_tasks.pop(phone, None)
            buffered = _message_buffers.pop(phone, [])
        if existing and not existing.done():
            existing.cancel()
        if buffered:
            try:
                await process_message(phone, "\n".join(buffered))
            except Exception as e:
                logger.error("Error drenando buffer antes de quick command: %s", e)
        await process_message(phone, text)
        return

    wait_seconds = get_timing("message_buffer_seconds")
    async with _buffer_lock:
        _message_buffers.setdefault(phone, []).append(text)
        existing = _buffer_tasks.get(phone)
        if existing and not existing.done():
            existing.cancel()
        _buffer_tasks[phone] = asyncio.create_task(_flush_buffer(phone, wait_seconds))


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
                        await buffer_and_process(phone, text)
                    elif msg.get("type") == "audio":
                        logger.info("Audio de %s", phone)
                        try:
                            media_id = msg["audio"]["id"]
                            text = await transcribe_audio(media_id)
                            logger.info("Transcripcion: %s", text)
                            await buffer_and_process(phone, text)
                        except Exception as e:
                            logger.error("Error transcribiendo audio: %s", e)
                            await send_whatsapp(phone, "No pude entender el audio. Intenta de nuevo o escribeme.")
    except Exception as e:
        logger.error("Webhook error: %s", e)
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {
        "status": "running",
        "version": "v3-turso",
        "time": datetime.now(tz).isoformat(),
        "calendar": "configurado" if gcal.is_configured() else "no configurado",
        "gmail": "configurado" if gmail.is_configured() else "no configurado",
    }


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
        h2 { font-size: 22px; margin-bottom: 4px; }
        .timings-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; background: white; border: 1px solid #d2d2d7; padding: 18px; border-radius: 8px; }
        .timing-field { display: flex; flex-direction: column; gap: 4px; }
        .timing-field label { font-size: 13px; font-weight: 500; }
        .timing-field .desc { color: #6e6e73; font-size: 12px; }
        .timing-field input { padding: 8px 10px; font-size: 14px; border: 1px solid #d2d2d7; border-radius: 6px; font-family: "Menlo", monospace; }
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
        <code>{{EVENTS_BLOCK}}</code> eventos proximas 24h &nbsp;
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

    <h2 style="margin-top: 40px;">Variables de tiempos</h2>
    <p class="sub">Cambios aplican en tiempo real. Sin redeploy.</p>

    <div class="timings-grid" id="timings-grid">__TIMINGS_INPUTS__</div>

    <div class="btn-row">
        <button class="btn-save" onclick="saveTimings()">Guardar tiempos</button>
        <button class="btn-reset" onclick="resetTimings()">Volver a defaults</button>
    </div>

    <div id="timings-status"></div>

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
        function setTimingsStatus(text, kind) {
            const s = document.getElementById("timings-status");
            s.textContent = text;
            s.className = "status-" + kind;
        }
        async function saveTimings() {
            setTimingsStatus("Guardando...", "loading");
            const inputs = document.querySelectorAll("#timings-grid input[data-key]");
            const values = {};
            for (const i of inputs) {
                const v = i.value.trim();
                if (v === "") continue;
                if (!/^-?\d+$/.test(v)) {
                    setTimingsStatus("Valor invalido en " + i.dataset.key + " (debe ser entero)", "err");
                    return;
                }
                values[i.dataset.key] = parseInt(v, 10);
            }
            try {
                const r = await fetch("/admin/timings", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({timings: values})
                });
                if (r.ok) {
                    setTimingsStatus("Tiempos guardados. Aplican en tiempo real.", "ok");
                } else {
                    setTimingsStatus("Error " + r.status + ": " + await r.text(), "err");
                }
            } catch(e) {
                setTimingsStatus("Error de red: " + e.message, "err");
            }
        }
        async function resetTimings() {
            if (!confirm("Volver todos los tiempos a sus defaults?")) return;
            setTimingsStatus("Reseteando...", "loading");
            try {
                const r = await fetch("/admin/timings/reset", {method: "POST"});
                if (r.ok) {
                    setTimingsStatus("Reseteado. Recarga la pagina para ver los defaults.", "ok");
                } else {
                    setTimingsStatus("Error " + r.status, "err");
                }
            } catch(e) {
                setTimingsStatus("Error de red: " + e.message, "err");
            }
        }
    </script>
</body>
</html>"""


TIMING_LABELS = [
    ("pre_event_min_1", "Pre-evento aviso 1 (min antes)", "El primero, el mas temprano"),
    ("pre_event_min_2", "Pre-evento aviso 2 (min antes)", ""),
    ("pre_event_min_3", "Pre-evento aviso 3 (min antes)", ""),
    ("pre_event_min_4", "Pre-evento aviso 4 (min antes)", "0 = al momento de empezar"),
    ("post_event_min", "Post-evento follow-up (min despues)", "Cuando preguntar ¿se dio?"),
    ("nudge_window_start_hour", "Ventana nudges - hora inicio", "0-23, hora local"),
    ("nudge_window_end_hour", "Ventana nudges - hora fin", "0-23, hora local"),
    ("nudge_cap_per_task", "Max nudges por tarea", "Despues de N, forzar decision"),
    ("followup_no_response_hours", "Followup sin respuesta (horas)", "Si no contestas, marca no_response"),
    ("message_buffer_seconds", "Buffer de mensajes (segundos)", "Espera N seg antes de procesar; si llegan mas mensajes, los junta"),
]


def _render_timings_inputs():
    parts = []
    for key, label, desc in TIMING_LABELS:
        val = get_timing(key)
        desc_html = ('<span class="desc">' + html_lib.escape(desc) + '</span>') if desc else ""
        parts.append(
            '<div class="timing-field">'
            '<label for="t-' + key + '">' + html_lib.escape(label) + '</label>'
            + desc_html +
            '<input id="t-' + key + '" data-key="' + key + '" type="number" value="' + str(val) + '">'
            '</div>'
        )
    return "\n".join(parts)


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
        .replace("__TIMINGS_INPUTS__", _render_timings_inputs())
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


@app.get("/admin/timings")
async def admin_get_timings(_: bool = Depends(admin_auth)):
    return {
        "timings": {key: get_timing(key) for key, _label, _desc in TIMING_LABELS},
        "defaults": {key: TIMING_DEFAULTS[key] for key, _label, _desc in TIMING_LABELS},
    }


@app.post("/admin/timings")
async def admin_save_timings(request: Request, _: bool = Depends(admin_auth)):
    body = await request.json()
    incoming = body.get("timings", {})
    if not isinstance(incoming, dict):
        raise HTTPException(400, "Formato invalido")
    allowed = {key for key, _label, _desc in TIMING_LABELS}
    saved = {}
    for key, raw_val in incoming.items():
        if key not in allowed:
            continue
        try:
            int_val = int(raw_val)
        except (TypeError, ValueError):
            raise HTTPException(400, "Valor no entero para " + key)
        if key in ("nudge_window_start_hour", "nudge_window_end_hour"):
            if not (0 <= int_val <= 23):
                raise HTTPException(400, "Hora fuera de rango (0-23) en " + key)
        elif int_val < 0:
            raise HTTPException(400, "Valor negativo no permitido en " + key)
        set_config(key, str(int_val))
        saved[key] = int_val
    return {"status": "ok", "saved": saved}


@app.post("/admin/timings/reset")
async def admin_reset_timings(_: bool = Depends(admin_auth)):
    for key, _label, _desc in TIMING_LABELS:
        delete_config(key)
    return {"status": "ok"}


@app.post("/admin/trigger-weekly")
async def admin_trigger_weekly(_: bool = Depends(admin_auth)):
    await weekly_review()
    return {"status": "ok", "sent_to": MY_PHONE_NUMBER}


@app.post("/admin/trigger-emails")
async def admin_trigger_emails(_: bool = Depends(admin_auth)):
    if not gmail.is_configured():
        return {"status": "gmail no configurado"}
    await check_new_emails()
    return {"status": "ok"}


@app.get("/admin/memories/stats")
async def admin_memories_stats(_: bool = Depends(admin_auth)):
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    by_role = conn.execute(
        "SELECT role, COUNT(*) FROM memories GROUP BY role"
    ).fetchall()
    return {
        "total": total,
        "by_role": {r[0]: r[1] for r in by_role},
    }


@app.get("/admin/memories/search")
async def admin_memories_search(q: str, _: bool = Depends(admin_auth)):
    return search_memories(q, limit=10, exclude_recent=0)
