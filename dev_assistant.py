"""
Comando /dev para desarrollar funciones desde WhatsApp.

Flujo:
1. /dev <descripcion>    -> propone cambio en dev_zone.py
2. (revisa el diff)      -> el bot responde con resumen + diff truncado
3. /dev ok               -> aplica, commitea via GitHub API, Railway redeploya
4. /dev no               -> descarta la propuesta pendiente
5. /dev ver              -> muestra contenido actual de dev_zone.py
6. /dev ayuda            -> ayuda del comando

Restricciones:
- Solo dev_zone.py es modificable.
- Propuestas expiran en 15 minutos.
- Cada cambio requiere confirmacion explicita.
- Se valida que el nuevo contenido compile como Python antes de aplicar.
"""

import base64
import difflib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEV_ZONE_FILE = "dev_zone.py"
PROPOSAL_TIMEOUT_MIN = 15
MODEL = os.environ.get("DEV_MODEL", "gpt-5.4-mini")

_REPO_ROOT = Path(__file__).resolve().parent
_DEV_ZONE_PATH = _REPO_ROOT / DEV_ZONE_FILE


SYSTEM_PROMPT = """Eres un asistente que modifica un unico archivo Python llamado dev_zone.py para un bot de WhatsApp.

REGLAS ABSOLUTAS:
1. Devuelves SIEMPRE un JSON con esta forma exacta, sin backticks ni texto adicional:
   {"summary": "que vas a cambiar en una frase corta", "new_content": "contenido COMPLETO del archivo dev_zone.py"}
2. new_content debe ser el archivo ENTERO, no un diff ni un fragmento. Incluye imports y docstrings.
3. El archivo debe seguir siendo Python valido.
4. Debe seguir existiendo la funcion:
   handle_custom_command(lower: str, original: str) -> Optional[str]
   que recibe el texto en minusculas y el original, y devuelve un string (respuesta WhatsApp) o None.
5. NO importes modulos del proyecto principal (app, gcal, gmail, dev_assistant). Solo stdlib.
6. Conserva las funciones/comandos que ya existan, salvo que el usuario pida quitarlas.
7. Si el usuario pide "/algo", ese trigger debe responder cuando lower == "/algo" o lower.startswith("/algo ").
8. Mantén las respuestas en el tono cercano y directo de Christian (CEO Los Lagos y YAVE)."""


def is_dev_command(text: str) -> bool:
    return text.strip().lower().startswith("/dev")


def parse_dev_command(text: str) -> tuple[str, str]:
    """Devuelve (subcomando, resto). Ej: '/dev ok' -> ('ok', ''); '/dev añade X' -> ('propose', 'añade X')."""
    body = text.strip()[len("/dev"):].strip()
    if not body:
        return "ayuda", ""
    lower = body.lower()
    if lower in ("ok", "si", "sí", "aplicar", "aplica"):
        return "ok", ""
    if lower in ("no", "cancela", "cancelar", "descarta", "descartar"):
        return "no", ""
    if lower in ("ver", "show", "estado"):
        return "ver", ""
    if lower in ("ayuda", "help", "?"):
        return "ayuda", ""
    if lower in ("pending", "pendiente"):
        return "pending", ""
    return "propose", body


def _read_dev_zone() -> str:
    if not _DEV_ZONE_PATH.exists():
        return ""
    return _DEV_ZONE_PATH.read_text(encoding="utf-8")


def _write_dev_zone(content: str) -> None:
    _DEV_ZONE_PATH.write_text(content, encoding="utf-8")


def _validate_python(content: str) -> tuple[bool, str]:
    try:
        compile(content, DEV_ZONE_FILE, "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError linea {e.lineno}: {e.msg}"


def _build_diff(old: str, new: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile="actual",
        tofile="nuevo",
        n=2,
    )
    return "".join(diff)


def _truncate_diff(diff: str, max_lines: int = 30) -> str:
    lines = diff.splitlines()
    if len(lines) <= max_lines:
        return diff or "(sin cambios visibles)"
    head = lines[:max_lines]
    return "\n".join(head) + f"\n... ({len(lines) - max_lines} lineas mas)"


def propose_change(openai_client, user_prompt: str) -> dict:
    """Llama al LLM para generar el nuevo contenido. Lanza excepcion si falla."""
    current = _read_dev_zone()
    user_msg = (
        "Contenido actual de dev_zone.py:\n"
        "```python\n" + current + "\n```\n\n"
        "Modificacion solicitada:\n" + user_prompt
    )
    response = openai_client.chat.completions.create(
        model=MODEL,
        max_completion_tokens=4096,
        temperature=0.2,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(raw)
    return {
        "summary": parsed.get("summary", "Sin resumen"),
        "new_content": parsed.get("new_content", ""),
        "old_content": current,
        "user_prompt": user_prompt,
        "created_at": datetime.utcnow().isoformat(),
    }


def is_proposal_expired(proposal: dict) -> bool:
    try:
        created = datetime.fromisoformat(proposal["created_at"])
    except (KeyError, ValueError):
        return True
    return (datetime.utcnow() - created).total_seconds() > PROPOSAL_TIMEOUT_MIN * 60


def format_proposal_for_whatsapp(proposal: dict) -> str:
    diff = _build_diff(proposal["old_content"], proposal["new_content"])
    short = _truncate_diff(diff)
    return (
        "🛠 *Cambio propuesto*\n"
        "_" + proposal["summary"] + "_\n\n"
        "*Diff:*\n```\n" + short + "\n```\n\n"
        "Responde */dev ok* para aplicar y desplegar.\n"
        "Responde */dev no* para descartar.\n"
        "(expira en " + str(PROPOSAL_TIMEOUT_MIN) + " min)"
    )


async def commit_via_github(new_content: str, summary: str) -> tuple[bool, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "")
    if not token or not repo:
        return False, "Falta GITHUB_TOKEN o GITHUB_REPO en env vars (Railway)."
    url = f"https://api.github.com/repos/{repo}/contents/{DEV_ZONE_FILE}"
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        sha = ""
        try:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                sha = r.json().get("sha", "")
            elif r.status_code != 404:
                return False, f"GitHub GET error {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, f"GitHub GET excepcion: {e}"

        body = {
            "message": "[via /dev] " + summary,
            "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
            "branch": "main",
        }
        if sha:
            body["sha"] = sha
        try:
            r = await client.put(url, headers=headers, json=body)
        except Exception as e:
            return False, f"GitHub PUT excepcion: {e}"
    if r.status_code in (200, 201):
        return True, ""
    return False, f"GitHub PUT error {r.status_code}: {r.text[:200]}"


async def apply_change(proposal: dict) -> tuple[bool, str]:
    new_content = proposal["new_content"]
    if not new_content.strip():
        return False, "❌ El LLM no devolvio contenido."
    ok, err = _validate_python(new_content)
    if not ok:
        return False, "❌ El codigo generado no es Python valido: " + err
    try:
        _write_dev_zone(new_content)
    except Exception as e:
        return False, "❌ No pude escribir el archivo localmente: " + str(e)
    ok, err = await commit_via_github(new_content, proposal["summary"])
    if not ok:
        return False, "⚠️ Cambio escrito en disco pero fallo el push: " + err
    return True, "✅ Aplicado, commiteado a GitHub. Railway redeploya en ~1-2 min."


def format_current_dev_zone() -> str:
    content = _read_dev_zone()
    if not content:
        return "📄 dev_zone.py esta vacio."
    lines = content.splitlines()
    if len(lines) > 40:
        content = "\n".join(lines[:40]) + f"\n... ({len(lines) - 40} lineas mas)"
    return "📄 *dev_zone.py actual:*\n```\n" + content + "\n```"


HELP_TEXT = (
    "🛠 *Comando /dev*\n\n"
    "Desarrolla funciones desde WhatsApp. Solo edita `dev_zone.py`.\n\n"
    "*Uso:*\n"
    "• `/dev <descripcion>` — proponer cambio\n"
    "• `/dev ok` — aplicar el cambio propuesto\n"
    "• `/dev no` — descartar propuesta\n"
    "• `/dev ver` — ver contenido actual\n"
    "• `/dev pending` — ver propuesta pendiente\n"
    "• `/dev ayuda` — esta ayuda\n\n"
    "*Ejemplos:*\n"
    "• `/dev agrega comando /mood que devuelva un mensaje motivacional aleatorio`\n"
    "• `/dev añade /frase que diga una frase de venta inmobiliaria`\n\n"
    "Cada cambio requiere `/dev ok` para aplicarse. Despues GitHub commit + Railway redeploy (~1-2 min)."
)
