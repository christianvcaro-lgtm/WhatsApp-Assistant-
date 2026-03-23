"""
ASISTENTE PERSONAL WHATSAPP - Christian
========================================
Bot de WhatsApp que captura tareas, ideas, recordatorios
y te da visibilidad de todo lo que tienes encima.

Stack: FastAPI + SQLite + OpenAI + WhatsApp Cloud API + APScheduler
"""

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

# ============================================================
# CONFIGURACIÓN - Variables de entorno
# ============================================================

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
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                priority TEXT DEFAULT 'media',
                category TEXT DEFAULT 'general',
                due_date TEXT,
                status TEXT DEFAULT 'pendiente',
                created_at TEXT DEFAULT (datetime('now')),
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                tags TEXT DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now')),
                reviewed INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                sent INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)

# ============================================================
# OPENAI - Intérprete de mensajes
# ============================================================

openai_client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """Eres el asistente personal de Christian por WhatsApp. Maneja YAVE (CRM WhatsApp para inmobiliarias) y Los Lagos (lotes campestres Cartagena). Te habla en español colombiano informal.

RESPONDE SOLO JSON VÁLIDO. Sin markdown, sin backticks.

{{
  "intent": "task|idea|reminder|query|complete|chat",
  "data": {{...}},
  "response": "Respuesta corta para WhatsApp"
}}

INTENTS:
- "tengo que","necesito","hay que","pendiente","hacer","tarea" → task
- "idea","se me ocurrió","qué tal si","podríamos" → idea
- "recuérdame","no se me olvide","avísame","a las X" → reminder
- "qué tengo","pendientes","resumen","cómo voy","mis tareas","mis ideas" → query
- "listo","hecho","ya hice","terminé","completé" → complete
- Otra cosa → chat (responde y sugiere si quiere guardarlo)

DATA:
task: {{"title":"corto","description":"detalle o null","priority":"alta​​​​​​​​​​​​​​​​
