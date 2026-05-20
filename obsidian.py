"""
obsidian.py - Sincronizacion con un vault de Obsidian alojado en un repo de GitHub.

El vault se mantiene en un repo de GitHub (privado). En el PC, el plugin
"Obsidian Git" hace commit/push automatico; este modulo lee y escribe ese mismo
repo via la API de GitHub, asi el bot y Obsidian comparten las mismas notas.

Variables de entorno:
    GITHUB_TOKEN   token de GitHub con acceso al repo del vault
    VAULT_REPO     repo del vault, formato "usuario/repo"

Sin estas dos, is_configured() retorna False y el bot sigue funcionando sin
tocar el vault.
"""
import os
import base64
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
VAULT_REPO = os.environ.get("VAULT_REPO", "")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/Bogota"))

# A que carpeta del vault va cada cosa segun su categoria.
TASK_FOLDER = {
    "loslagos": "proyectos/los-lagos",
    "yave": "proyectos/yave",
    "personal": "tareas",
    "general": "tareas",
}
IDEA_FOLDER = {
    "loslagos": "proyectos/los-lagos",
    "yave": "proyectos/yave",
    "personal": "inbox",
    "general": "inbox",
}
TASK_FOLDERS = ["tareas", "proyectos/los-lagos", "proyectos/yave"]


def is_configured() -> bool:
    return bool(GITHUB_TOKEN and VAULT_REPO)


def _headers():
    return {
        "Authorization": "token " + GITHUB_TOKEN,
        "Accept": "application/vnd.github.v3+json",
    }


def _fm(value) -> str:
    """Limpia un valor para una linea de frontmatter (key: value)."""
    return str(value or "").replace("\n", " ").replace("\r", " ").strip()


def parse_frontmatter(content: str):
    """Separa el frontmatter YAML simple del cuerpo de una nota markdown."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    meta = {}
    for line in parts[1].strip().split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip().strip('"').strip("'")
    return meta, parts[2].strip()


async def _get_sha(http, path):
    url = "https://api.github.com/repos/" + VAULT_REPO + "/contents/" + path
    try:
        resp = await http.get(url, headers=_headers(), timeout=10)
        if resp.status_code == 200:
            return resp.json().get("sha")
    except Exception as e:
        logger.error("obsidian _get_sha error %s: %s", path, e)
    return None


async def upsert_note(content: str, folder: str, filename: str):
    """Crea o actualiza una nota markdown en el vault."""
    if not is_configured():
        return
    path = folder + "/" + filename
    url = "https://api.github.com/repos/" + VAULT_REPO + "/contents/" + path
    async with httpx.AsyncClient() as http:
        sha = await _get_sha(http, path)
        payload = {
            "message": ("update: " if sha else "capture: ") + filename,
            "content": base64.b64encode(content.encode()).decode(),
        }
        if sha:
            payload["sha"] = sha
        try:
            resp = await http.put(url, headers=_headers(), json=payload, timeout=10)
            if resp.status_code in (200, 201):
                logger.info("obsidian: nota guardada en %s", path)
            else:
                logger.error("obsidian upsert %s -> HTTP %s", path, resp.status_code)
        except Exception as e:
            logger.error("obsidian upsert error %s: %s", path, e)


async def read_folder(folder: str):
    """Lee todos los .md de una carpeta del vault. Retorna [] si no existe."""
    if not is_configured():
        return []
    url = "https://api.github.com/repos/" + VAULT_REPO + "/contents/" + folder
    files = []
    async with httpx.AsyncClient() as http:
        try:
            resp = await http.get(url, headers=_headers(), timeout=10)
            if resp.status_code != 200:
                return []
            for item in resp.json():
                name = item.get("name", "")
                if not name.endswith(".md") or name == "README.md":
                    continue
                file_resp = await http.get(item["url"], headers=_headers(), timeout=10)
                if file_resp.status_code == 200:
                    raw = base64.b64decode(file_resp.json()["content"]).decode()
                    files.append({"name": name, "path": item.get("path", ""), "content": raw})
        except Exception as e:
            logger.error("obsidian read_folder %s error: %s", folder, e)
    return files


async def capture_task(task_id, data):
    """Guarda una tarea nueva como nota en el vault."""
    if not is_configured():
        return
    now = datetime.now(TZ)
    category = data.get("category", "general")
    title = data.get("title", "")
    description = data.get("description") or ""
    fm = [
        "fecha: " + now.strftime("%Y-%m-%d"),
        "tipo: tarea",
        "titulo: " + _fm(title),
        "prioridad: " + _fm(data.get("priority", "media")),
        "proyecto: " + _fm(category),
        "estado: pendiente",
    ]
    if description:
        fm.append("descripcion: " + _fm(description))
    if data.get("due_date"):
        fm.append("vence: " + _fm(data["due_date"]))
    md = (
        "---\n" + "\n".join(fm) + "\n---\n\n"
        "# " + title + "\n\n"
        + (description + "\n\n" if description else "")
        + "---\n*Capturado desde WhatsApp el " + now.strftime("%Y-%m-%d %H:%M") + "*\n"
    )
    folder = TASK_FOLDER.get(category, "tareas")
    filename = now.strftime("%Y-%m-%d-%H%M") + "-tarea-" + str(task_id) + ".md"
    await upsert_note(md, folder, filename)


async def capture_idea(idea_id, data):
    """Guarda una idea nueva como nota en el vault."""
    if not is_configured():
        return
    now = datetime.now(TZ)
    category = data.get("category", "general")
    content = data.get("content", "")
    md = (
        "---\n"
        "fecha: " + now.strftime("%Y-%m-%d") + "\n"
        "tipo: idea\n"
        "proyecto: " + _fm(category) + "\n"
        "---\n\n"
        "# " + content[:60] + "\n\n"
        + content + "\n\n"
        "---\n*Capturado desde WhatsApp el " + now.strftime("%Y-%m-%d %H:%M") + "*\n"
    )
    folder = IDEA_FOLDER.get(category, "inbox")
    filename = now.strftime("%Y-%m-%d-%H%M") + "-idea-" + str(idea_id) + ".md"
    await upsert_note(md, folder, filename)


async def capture_reminder(reminder_id, data):
    """Guarda un recordatorio nuevo como nota en el vault."""
    if not is_configured():
        return
    now = datetime.now(TZ)
    message = data.get("message", "")
    remind_at = data.get("remind_at", "")
    md = (
        "---\n"
        "fecha: " + now.strftime("%Y-%m-%d") + "\n"
        "tipo: recordatorio\n"
        "mensaje: " + _fm(message) + "\n"
        "recordar_en: " + _fm(remind_at) + "\n"
        "enviado: false\n"
        "---\n\n"
        "# " + message + "\n\n"
        "Recordar el " + remind_at + "\n"
    )
    filename = now.strftime("%Y-%m-%d-%H%M") + "-recordatorio-" + str(reminder_id) + ".md"
    await upsert_note(md, "recordatorios", filename)


async def capture_context(key, value):
    """Guarda (o actualiza) una nota de contexto aprendido en el vault."""
    if not is_configured():
        return
    now = datetime.now(TZ)
    md = (
        "---\n"
        "fecha: " + now.strftime("%Y-%m-%d") + "\n"
        "tipo: contexto\n"
        "clave: " + _fm(key) + "\n"
        "valor: " + _fm(value) + "\n"
        "---\n\n"
        "# " + key + "\n\n"
        + value + "\n"
    )
    safe_key = key.lower().replace(" ", "-").replace("/", "-")[:40]
    await upsert_note(md, "contexto", "contexto-" + safe_key + ".md")


async def _set_task_estado(title, new_estado, date_field):
    """Busca la nota de una tarea por titulo y le cambia el estado."""
    if not is_configured() or not title:
        return
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    target = title.strip().lower()
    for folder in TASK_FOLDERS:
        for note in await read_folder(folder):
            meta, _ = parse_frontmatter(note["content"])
            if meta.get("tipo") != "tarea":
                continue
            if meta.get("titulo", "").strip().lower() != target:
                continue
            updated = note["content"].replace("estado: pendiente", "estado: " + new_estado)
            updated = updated.replace(
                "---\n\n# ", date_field + ": " + today + "\n---\n\n# ", 1
            )
            await upsert_note(updated, folder, note["name"])
            return


async def mark_task_completed(title):
    await _set_task_estado(title, "completada", "completado")


async def mark_task_killed(title):
    await _set_task_estado(title, "descartada", "descartado")


async def search_notes(query: str, limit: int = 5):
    """Busca el query en los .md del vault via GitHub code search.

    Retorna lista de dicts con 'name', 'path', 'snippet'. Lista vacia si no
    hay vault configurado, si la query esta vacia, o si la busqueda falla.
    """
    if not is_configured() or not query.strip():
        return []
    q = query.strip() + " repo:" + VAULT_REPO + " extension:md"
    url = "https://api.github.com/search/code"
    headers = dict(_headers())
    headers["Accept"] = "application/vnd.github.text-match+json"
    async with httpx.AsyncClient() as http:
        try:
            resp = await http.get(
                url, headers=headers, params={"q": q, "per_page": limit}, timeout=15
            )
            if resp.status_code != 200:
                logger.warning("obsidian search '%s' -> HTTP %s", query, resp.status_code)
                return []
            results = []
            for item in resp.json().get("items", []):
                snippet = ""
                matches = item.get("text_matches") or []
                if matches:
                    snippet = (matches[0].get("fragment") or "").strip()
                results.append({
                    "name": item.get("name", ""),
                    "path": item.get("path", ""),
                    "snippet": snippet[:240],
                })
            return results
        except Exception as e:
            logger.error("obsidian search error: %s", e)
            return []
