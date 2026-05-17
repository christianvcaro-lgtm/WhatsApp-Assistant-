"""
gmail.py - Lectura de Gmail (solo lectura).

Usa las MISMAS credenciales de Google que gcal.py. Para que funcione, el
GOOGLE_REFRESH_TOKEN debe haberse generado con el scope gmail.readonly
incluido: corre auth_helper.py (que ya pide Calendar + Gmail) y reemplaza
el token viejo en Railway por el nuevo.

Variables de entorno (compartidas con gcal.py):
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN

Uso como script (verificar conexion / ver correos recientes):
    python gmail.py
"""
import os
import html
import logging
from email.utils import parseaddr
from email.header import decode_header

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def is_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN)


def get_gmail_service():
    if not is_configured():
        return None
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_mime_header(value: str) -> str:
    """Decodifica headers MIME (=?UTF-8?B?...?=) - asuntos/nombres con tildes."""
    if not value:
        return ""
    try:
        parts = decode_header(value)
    except Exception:
        return value
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def list_message_ids(service, query: str = "in:inbox", max_results: int = 30) -> list:
    """IDs de los mensajes que matchean `query` (sintaxis de busqueda de Gmail,
    ej: "in:inbox", "is:unread", "is:starred", "newer_than:1d"). Una sola
    llamada a la API; no descarga el contenido de los mensajes."""
    if not service:
        return []
    try:
        listing = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
    except HttpError as e:
        logger.error("gmail list error: %s", e)
        return []
    except Exception as e:
        logger.error("gmail list unexpected error: %s", e)
        return []
    return [m.get("id") for m in listing.get("messages", []) if m.get("id")]


def get_email(service, message_id: str):
    """Un correo por ID. Devuelve dict (id, thread_id, from_name, from_email,
    subject, date, snippet, unread) o None si falla. Trae metadata + snippet,
    no el cuerpo completo: suficiente para clasificar y liviano en cuota."""
    if not service:
        return None
    try:
        full = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
    except Exception as e:
        logger.error("gmail get error (%s): %s", message_id, e)
        return None
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in full.get("payload", {}).get("headers", [])
    }
    from_name, from_email = parseaddr(headers.get("from", ""))
    return {
        "id": full.get("id", message_id),
        "thread_id": full.get("threadId", ""),
        "from_name": _decode_mime_header(from_name) or from_email,
        "from_email": from_email,
        "subject": _decode_mime_header(headers.get("subject", "")) or "(sin asunto)",
        "date": headers.get("date", ""),
        "snippet": html.unescape(full.get("snippet", "")),
        "unread": "UNREAD" in full.get("labelIds", []),
    }


def fetch_recent_emails(query: str = "in:inbox", max_results: int = 25) -> list:
    """Lista correos completos que matchean `query`. Helper de alto nivel:
    combina list_message_ids + get_email para cada uno."""
    service = get_gmail_service()
    if not service:
        return []
    emails = []
    for msg_id in list_message_ids(service, query, max_results):
        email = get_email(service, msg_id)
        if email:
            emails.append(email)
    return emails


if __name__ == "__main__":
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        print("Faltan GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET en el entorno.")
        raise SystemExit(1)
    if not GOOGLE_REFRESH_TOKEN:
        print("")
        print("Pega tu refresh token (la linea larga que empieza con '1//')")
        print("y dale Enter:")
        try:
            GOOGLE_REFRESH_TOKEN = input("token> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            print("Cancelado.")
            raise SystemExit(1)
    if not GOOGLE_REFRESH_TOKEN:
        print("No se recibio ningun token. Cancelado.")
        raise SystemExit(1)
    print("")
    print("Conectando a Gmail...")
    items = fetch_recent_emails(query="in:inbox", max_results=25)
    if not items:
        print("")
        print("No se obtuvieron correos. Causas posibles:")
        print("  - El token quedo mal copiado (incompleto o con espacios)")
        print("  - El token se genero sin el permiso de Gmail: re-corre")
        print("    auth_helper.py y acepta TODOS los permisos.")
        raise SystemExit(1)
    print("")
    print(str(len(items)) + " correos recientes en el inbox (* = sin leer):")
    print("")
    for idx, e in enumerate(items, 1):
        flag = "* " if e["unread"] else "  "
        print(flag + str(idx) + ". " + e["from_name"] + "  <" + e["from_email"] + ">")
        print("     " + e["subject"])
        print("     " + e["snippet"][:140])
        print("")
