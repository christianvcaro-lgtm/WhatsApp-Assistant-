# “””
ASISTENTE PERSONAL WHATSAPP - Christian

Bot de WhatsApp que captura tareas, ideas, recordatorios
y te da visibilidad de todo lo que tienes encima.

Stack: FastAPI + SQLite + Claude API + WhatsApp Cloud API + APScheduler
“””

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
from anthropic import Anthropic

# ============================================================

# CONFIGURACIÓN - Variables de entorno

# ============================================================

WHATSAPP_TOKEN = os.environ.get(“WHATSAPP_TOKEN”, “”)
WHATSAPP_PHONE_ID = os.environ.get(“WHATSAPP_PHONE_ID”, “653078644555574”)
VERIFY_TOKEN = os.environ.get(“VERIFY_TOKEN”, “mi_asistente_personal_2024”)
ANTHROPIC_API_KEY = os.environ.get(“ANTHROPIC_API_KEY”, “”)
MY_PHONE_NUMBER = os.environ.get(“MY_PHONE_NUMBER”, “”)  # Tu num: 573001234567
TIMEZONE = os.environ.get(“TIMEZONE”, “America/Bogota”)
DB_PATH = os.environ.get(“DB_PATH”, “assistant.db”)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(**name**)
tz = ZoneInfo(TIMEZONE)

# ============================================================

# BASE DE DATOS

# ============================================================

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
conn.executescript(”””
CREATE TABLE IF NOT EXISTS tasks (
id INTEGER PRIMARY KEY AUTOINCREMENT,
title TEXT NOT NULL,
description TEXT,
priority TEXT DEFAULT ‘media’,
category TEXT DEFAULT ‘general’,
due_date TEXT,
status TEXT DEFAULT ‘pendiente’,
created_at TEXT DEFAULT (datetime(‘now’)),
completed_at TEXT
);
CREATE TABLE IF NOT EXISTS ideas (
id INTEGER PRIMARY KEY AUTOINCREMENT,
content TEXT NOT NULL,
category TEXT DEFAULT ‘general’,
tags TEXT DEFAULT ‘[]’,
created_at TEXT DEFAULT (datetime(‘now’)),
reviewed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reminders (
id INTEGER PRIMARY KEY AUTOINCREMENT,
message TEXT NOT NULL,
remind_at TEXT NOT NULL,
sent INTEGER DEFAULT 0,
created_at TEXT DEFAULT (datetime(‘now’))
);
“””)

# ============================================================

# CLAUDE - Intérprete de mensajes

# ============================================================

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = “”“Eres el asistente personal de Christian por WhatsApp. Maneja YAVE (CRM WhatsApp para inmobiliarias) y Los Lagos (lotes campestres Cartagena). Te habla en español colombiano informal.

RESPONDE SOLO JSON VÁLIDO. Sin markdown, sin backticks.

{{
“intent”: “task|idea|reminder|query|complete|chat”,
“data”: {{…}},
“response”: “Respuesta corta para WhatsApp”
}}

INTENTS:

- “tengo que”,“necesito”,“hay que”,“pendiente”,“hacer”,“tarea” → task
- “idea”,“se me ocurrió”,“qué tal si”,“podríamos” → idea
- “recuérdame”,“no se me olvide”,“avísame”,“a las X” → reminder
- “qué tengo”,“pendientes”,“resumen”,“cómo voy”,“mis tareas”,“mis ideas” → query
- “listo”,“hecho”,“ya hice”,“terminé”,“completé” → complete
- Otra cosa → chat (responde y sugiere si quiere guardarlo)

DATA:
task: {{“title”:“corto”,“description”:“detalle o null”,“priority”:“alta|media|baja”,“category”:“yave|loslagos|personal|general”,“due_date”:“YYYY-MM-DD o null”}}
idea: {{“content”:“la idea”,“category”:“yave|loslagos|personal|general”,“tags”:[“tag1”]}}
reminder: {{“message”:“qué recordar”,“remind_at”:“YYYY-MM-DD HH:MM”}}
query: {{“query_type”:“pending_tasks|ideas|today|overdue|category”,“category”:“yave|loslagos|personal|general o null”}}
complete: {{“search_term”:“texto para buscar la tarea”}}
chat: {{}}

PRIORIDAD: urgente/hoy/asap→alta | fecha<3días→alta | sin fecha→media | cuando pueda→baja
CATEGORÍA: WhatsApp,CRM,API,Meta,código→yave | lotes,Cartagena,ventas→loslagos | gym,padel,familia→personal | resto→general

FECHA: {current_date} | HORA: {current_time}

Para reminders: calcula fecha/hora real. “mañana a las 8” = fecha de mañana 08:00. “en 2 horas” = suma desde hora actual.”””

async def interpret_message(text: str) -> dict:
now = datetime.now(tz)
prompt = SYSTEM_PROMPT.replace(”{current_date}”, now.strftime(”%Y-%m-%d”)).replace(”{current_time}”, now.strftime(”%H:%M”))
try:
response = anthropic_client.messages.create(
model=“claude-sonnet-4-20250514”,
max_tokens=1024,
system=prompt,
messages=[{“role”: “user”, “content”: text}]
)
raw = response.content[0].text.strip().replace(”`json", "").replace("`”, “”).strip()
return json.loads(raw)
except json.JSONDecodeError:
logger.error(f”JSON parse error. Raw: {raw}”)
return {“intent”: “chat”, “data”: {}, “response”: “No entendí bien, ¿puedes reformularlo?”}
except Exception as e:
logger.error(f”Claude error: {e}”)
return {“intent”: “chat”, “data”: {}, “response”: “Error procesando. Intenta de nuevo.”}

# ============================================================

# ACCIONES DE BASE DE DATOS

# ============================================================

def add_task(data: dict) -> int:
with get_db() as conn:
cur = conn.execute(
“INSERT INTO tasks (title,description,priority,category,due_date) VALUES (?,?,?,?,?)”,
(data.get(“title”, “Sin título”), data.get(“description”),
data.get(“priority”, “media”), data.get(“category”, “general”), data.get(“due_date”))
)
return cur.lastrowid

def add_idea(data: dict) -> int:
with get_db() as conn:
cur = conn.execute(
“INSERT INTO ideas (content,category,tags) VALUES (?,?,?)”,
(data.get(“content”, “”), data.get(“category”, “general”), json.dumps(data.get(“tags”, [])))
)
return cur.lastrowid

def add_reminder(data: dict) -> int:
with get_db() as conn:
cur = conn.execute(
“INSERT INTO reminders (message,remind_at) VALUES (?,?)”,
(data.get(“message”, “”), data.get(“remind_at”, “”))
)
return cur.lastrowid

def complete_task(search_term: str) -> Optional[str]:
with get_db() as conn:
tasks = conn.execute(
“SELECT id,title FROM tasks WHERE status=‘pendiente’ AND LOWER(title) LIKE ?”,
(f”%{search_term.lower()}%”,)
).fetchall()
if len(tasks) == 1:
conn.execute(“UPDATE tasks SET status=‘completada’,completed_at=datetime(‘now’) WHERE id=?”, (tasks[0][“id”],))
return tasks[0][“title”]
elif len(tasks) > 1:
return “MULTIPLE:” + “, “.join(t[“title”] for t in tasks)
return None

def get_pending_tasks(category: str = None) -> list:
with get_db() as conn:
if category and category != “null”:
rows = conn.execute(
“SELECT * FROM tasks WHERE status=‘pendiente’ AND category=? ORDER BY CASE priority WHEN ‘alta’ THEN 1 WHEN ‘media’ THEN 2 ELSE 3 END, due_date”,
(category,)
).fetchall()
else:
rows = conn.execute(
“SELECT * FROM tasks WHERE status=‘pendiente’ ORDER BY CASE priority WHEN ‘alta’ THEN 1 WHEN ‘media’ THEN 2 ELSE 3 END, due_date”
).fetchall()
return [dict(r) for r in rows]

def get_overdue_tasks() -> list:
today = datetime.now(tz).strftime(”%Y-%m-%d”)
with get_db() as conn:
rows = conn.execute(
“SELECT * FROM tasks WHERE status=‘pendiente’ AND due_date<? AND due_date IS NOT NULL”, (today,)
).fetchall()
return [dict(r) for r in rows]

def get_recent_ideas(limit: int = 10) -> list:
with get_db() as conn:
rows = conn.execute(“SELECT * FROM ideas ORDER BY created_at DESC LIMIT ?”, (limit,)).fetchall()
return [dict(r) for r in rows]

def get_today_summary() -> dict:
today = datetime.now(tz).strftime(”%Y-%m-%d”)
with get_db() as conn:
pending = conn.execute(“SELECT COUNT(*) as c FROM tasks WHERE status=‘pendiente’”).fetchone()[“c”]
high = conn.execute(“SELECT COUNT(*) as c FROM tasks WHERE status=‘pendiente’ AND priority=‘alta’”).fetchone()[“c”]
due_today = conn.execute(“SELECT * FROM tasks WHERE status=‘pendiente’ AND due_date=?”, (today,)).fetchall()
overdue = conn.execute(“SELECT * FROM tasks WHERE status=‘pendiente’ AND due_date<? AND due_date IS NOT NULL”, (today,)).fetchall()
completed = conn.execute(“SELECT COUNT(*) as c FROM tasks WHERE completed_at LIKE ?”, (f”{today}%”,)).fetchone()[“c”]
ideas = conn.execute(“SELECT COUNT(*) as c FROM ideas WHERE created_at LIKE ?”, (f”{today}%”,)).fetchone()[“c”]
return {
“pending”: pending, “high_priority”: high,
“due_today”: [dict(r) for r in due_today], “overdue”: [dict(r) for r in overdue],
“completed_today”: completed, “ideas_today”: ideas
}

def get_pending_reminders() -> list:
now = datetime.now(tz).strftime(”%Y-%m-%d %H:%M”)
with get_db() as conn:
rows = conn.execute(“SELECT * FROM reminders WHERE sent=0 AND remind_at<=?”, (now,)).fetchall()
return [dict(r) for r in rows]

def mark_reminder_sent(rid: int):
with get_db() as conn:
conn.execute(“UPDATE reminders SET sent=1 WHERE id=?”, (rid,))

# ============================================================

# FORMATEADORES

# ============================================================

P_EMOJI = {“alta”: “🔴”, “media”: “🟡”, “baja”: “🟢”}
C_EMOJI = {“yave”: “🤖”, “loslagos”: “🏡”, “personal”: “👤”, “general”: “📌”}

def format_tasks(tasks: list) -> str:
if not tasks:
return “✅ Sin tareas pendientes.”
lines = [“📋 *PENDIENTES*\n”]
for t in tasks:
p = P_EMOJI.get(t[“priority”], “⚪”)
c = C_EMOJI.get(t[“category”], “📌”)
due = f” ⏰{t[‘due_date’]}” if t.get(“due_date”) else “”
lines.append(f”{p}{c} {t[‘title’]}{due}”)
return “\n”.join(lines)

def format_ideas(ideas: list) -> str:
if not ideas:
return “💡 No hay ideas guardadas.”
lines = [“💡 *IDEAS RECIENTES*\n”]
for i in ideas:
c = C_EMOJI.get(i[“category”], “📌”)
lines.append(f”{c} {i[‘content’]}”)
return “\n”.join(lines)

def format_summary(s: dict) -> str:
lines = [
“📊 *RESUMEN DEL DÍA*\n”,
f”📋 Pendientes: *{s[‘pending’]}* ({s[‘high_priority’]} urgentes)”,
f”✅ Completadas hoy: *{s[‘completed_today’]}*”,
f”💡 Ideas hoy: *{s[‘ideas_today’]}*”,
]
if s[“overdue”]:
lines.append(f”\n⚠️ *VENCIDAS ({len(s[‘overdue’])}):*”)
for t in s[“overdue”]:
lines.append(f”  🔴 {t[‘title’]} (vencía {t[‘due_date’]})”)
if s[“due_today”]:
lines.append(f”\n📅 *PARA HOY ({len(s[‘due_today’])}):*”)
for t in s[“due_today”]:
lines.append(f”  ➡️ {t[‘title’]}”)
return “\n”.join(lines)

# ============================================================

# WHATSAPP API

# ============================================================

async def send_whatsapp(to: str, message: str):
url = f”https://graph.facebook.com/v22.0/{WHATSAPP_PHONE_ID}/messages”
headers = {“Authorization”: f”Bearer {WHATSAPP_TOKEN}”, “Content-Type”: “application/json”}
payload = {“messaging_product”: “whatsapp”, “to”: to, “type”: “text”, “text”: {“body”: message}}
async with httpx.AsyncClient() as http:
try:
resp = await http.post(url, headers=headers, json=payload, timeout=10)
resp.raise_for_status()
logger.info(f”✅ Mensaje enviado a {to}”)
except Exception as e:
logger.error(f”❌ Error enviando WhatsApp: {e}”)

# ============================================================

# PROCESADOR PRINCIPAL

# ============================================================

async def process_message(phone: str, text: str):
# Atajos rápidos sin gastar tokens de Claude
lower = text.strip().lower()
if lower in [“resumen”, “cómo voy”, “como voy”, “status”]:
return await send_whatsapp(phone, format_summary(get_today_summary()))
if lower in [“pendientes”, “tareas”, “mis tareas”]:
return await send_whatsapp(phone, format_tasks(get_pending_tasks()))
if lower in [“ideas”, “mis ideas”]:
return await send_whatsapp(phone, format_ideas(get_recent_ideas()))
if lower == “ayuda”:
return await send_whatsapp(phone,
“🤖 *COMANDOS RÁPIDOS*\n\n”
“📋 *pendientes* → ver tareas\n”
“💡 *ideas* → ver ideas\n”
“📊 *resumen* → resumen del día\n\n”
“O simplemente escríbeme natural:\n”
“• *Tengo que llamar a Juan mañana*\n”
“• *Idea: hacer webinar de YAVE*\n”
“• *Recuérdame a las 3pm revisar métricas*\n”
“• *Ya hice lo de Juan*”
)

```
# Claude interpreta
result = await interpret_message(text)
intent = result.get("intent", "chat")
data = result.get("data", {})
response_text = result.get("response", "👍")

if intent == "task":
    tid = add_task(data)
    p = P_EMOJI.get(data.get("priority", "media"), "🟡")
    c = C_EMOJI.get(data.get("category", "general"), "📌")
    msg = f"✅ Tarea #{tid} guardada\n{p}{c} *{data.get('title', '')}*"
    if data.get("due_date"):
        msg += f"\n⏰ Para: {data['due_date']}"
    await send_whatsapp(phone, msg)

elif intent == "idea":
    iid = add_idea(data)
    await send_whatsapp(phone, f"💡 Idea #{iid} guardada\n_{data.get('content', '')}_")

elif intent == "reminder":
    rid = add_reminder(data)
    await send_whatsapp(phone, f"⏰ Recordatorio #{rid}\n_{data.get('message', '')}_\n🕐 {data.get('remind_at', '')}")

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
        await send_whatsapp(phone, "⚠️ *VENCIDAS*\n" + format_tasks(tasks) if tasks else "✅ Nada vencido.")
    else:
        await send_whatsapp(phone, response_text)

elif intent == "complete":
    search = data.get("search_term", "")
    result = complete_task(search)
    if result and result.startswith("MULTIPLE:"):
        await send_whatsapp(phone, f"🤔 Varias coinciden:\n{result[9:]}\n\nSé más específico.")
    elif result:
        await send_whatsapp(phone, f"🎉 *Completada:* {result}")
    else:
        await send_whatsapp(phone, f"🔍 No encontré \"{search}\". Escribe *pendientes* para ver la lista.")

else:
    await send_whatsapp(phone, response_text)
```

# ============================================================

# SCHEDULER

# ============================================================

scheduler = AsyncIOScheduler(timezone=TIMEZONE)

async def check_reminders():
for r in get_pending_reminders():
if MY_PHONE_NUMBER:
await send_whatsapp(MY_PHONE_NUMBER, f”⏰ *RECORDATORIO*\n\n{r[‘message’]}”)
mark_reminder_sent(r[“id”])

async def morning_summary():
if not MY_PHONE_NUMBER:
return
summary = get_today_summary()
high = [t for t in get_pending_tasks() if t[“priority”] == “alta”]
msg = f”☀️ *Buenos días, Christian*\n\n{format_summary(summary)}”
if high:
msg += “\n\n🎯 *FOCUS HOY:*”
for t in high[:3]:
msg += f”\n  {C_EMOJI.get(t[‘category’], ‘📌’)} {t[‘title’]}”
await send_whatsapp(MY_PHONE_NUMBER, msg)

async def evening_review():
if not MY_PHONE_NUMBER:
return
s = get_today_summary()
msg = f”🌙 *Cierre del día*\n\n✅ Completaste *{s[‘completed_today’]}* hoy\n📋 Quedan *{s[‘pending’]}* pendientes”
if s[“overdue”]:
msg += f”\n⚠️ *{len(s[‘overdue’])}* vencidas”
await send_whatsapp(MY_PHONE_NUMBER, msg)

# ============================================================

# FASTAPI

# ============================================================

app = FastAPI(title=“Asistente Personal WhatsApp”)

@app.on_event(“startup”)
async def startup():
init_db()
scheduler.add_job(check_reminders, IntervalTrigger(minutes=1), id=“reminders”)
scheduler.add_job(morning_summary, CronTrigger(hour=7, minute=0), id=“morning”)
scheduler.add_job(evening_review, CronTrigger(hour=21, minute=0), id=“evening”)
scheduler.start()
logger.info(“🚀 Asistente Personal iniciado”)

@app.on_event(“shutdown”)
async def shutdown():
scheduler.shutdown()

@app.get(”/webhook”)
async def verify_webhook(
hub_mode: str = Query(None, alias=“hub.mode”),
hub_token: str = Query(None, alias=“hub.verify_token”),
hub_challenge: str = Query(None, alias=“hub.challenge”),
):
if hub_mode == “subscribe” and hub_token == VERIFY_TOKEN:
logger.info(“✅ Webhook verificado”)
return Response(content=hub_challenge, media_type=“text/plain”)
return Response(status_code=403)

@app.post(”/webhook”)
async def receive_webhook(request: Request):
body = await request.json()
try:
for entry in body.get(“entry”, []):
for change in entry.get(“changes”, []):
messages = change.get(“value”, {}).get(“messages”, [])
for msg in messages:
if msg.get(“type”) == “text”:
phone = msg[“from”]
text = msg[“text”][“body”]
logger.info(f”📩 {phone}: {text}”)
if MY_PHONE_NUMBER and phone != MY_PHONE_NUMBER:
logger.warning(f”⚠️ Número no autorizado: {phone}”)
continue
await process_message(phone, text)
except Exception as e:
logger.error(f”Webhook error: {e}”)
return {“status”: “ok”}

@app.get(”/health”)
async def health():
return {“status”: “running”, “time”: datetime.now(tz).isoformat()}

@app.get(”/tasks”)
async def api_tasks():
return get_pending_tasks()

@app.get(”/ideas”)
async def api_ideas():
return get_recent_ideas(20)

@app.get(”/summary”)
async def api_summary():
return get_today_summary()
