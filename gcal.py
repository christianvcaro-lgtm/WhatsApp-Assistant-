"""
gcal.py - Google Calendar integration.

Variables de entorno requeridas (sin estas, is_configured() retorna False
y el resto del bot sigue funcionando sin Calendar):
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN

Opcionales (con defaults):
    GOOGLE_TIMEZONE       (default: America/Bogota)
    GOOGLE_CALENDAR_ID    (default: primary)
"""
import os
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
GOOGLE_TIMEZONE = os.environ.get("GOOGLE_TIMEZONE", "America/Bogota")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def is_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN)


def get_calendar_service():
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
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def list_upcoming_events(minutes_ahead: int = 35) -> list:
    service = get_calendar_service()
    if not service:
        return []
    tz = ZoneInfo(GOOGLE_TIMEZONE)
    now = datetime.now(tz)
    time_min = now.isoformat()
    time_max = (now + timedelta(minutes=minutes_ahead)).isoformat()
    try:
        result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()
        events = result.get("items", [])
        return [e for e in events if "dateTime" in e.get("start", {})]
    except HttpError as e:
        logger.error("gcal list error: %s", e)
        return []
    except Exception as e:
        logger.error("gcal list unexpected error: %s", e)
        return []


def list_events_next_hours(hours_ahead: int = 24) -> list:
    service = get_calendar_service()
    if not service:
        return []
    tz = ZoneInfo(GOOGLE_TIMEZONE)
    now = datetime.now(tz)
    time_min = now.isoformat()
    time_max = (now + timedelta(hours=hours_ahead)).isoformat()
    try:
        result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=30,
        ).execute()
        events = result.get("items", [])
        return [e for e in events if "dateTime" in e.get("start", {})]
    except HttpError as e:
        logger.error("gcal list_next_hours error: %s", e)
        return []
    except Exception as e:
        logger.error("gcal list_next_hours unexpected error: %s", e)
        return []


def list_events_tomorrow() -> list:
    service = get_calendar_service()
    if not service:
        return []
    tz = ZoneInfo(GOOGLE_TIMEZONE)
    now = datetime.now(tz)
    tomorrow_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_after = tomorrow_start + timedelta(days=1)
    time_min = tomorrow_start.isoformat()
    time_max = day_after.isoformat()
    try:
        result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=30,
        ).execute()
        events = result.get("items", [])
        return [e for e in events if "dateTime" in e.get("start", {})]
    except HttpError as e:
        logger.error("gcal list_tomorrow error: %s", e)
        return []
    except Exception as e:
        logger.error("gcal list_tomorrow unexpected error: %s", e)
        return []


def is_event_active_now() -> bool:
    service = get_calendar_service()
    if not service:
        return False
    tz = ZoneInfo(GOOGLE_TIMEZONE)
    now = datetime.now(tz)
    time_min = (now - timedelta(hours=2)).isoformat()
    time_max = now.isoformat()
    try:
        result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=10,
        ).execute()
        events = result.get("items", [])
        for e in events:
            start = e.get("start", {}).get("dateTime", "")
            end = e.get("end", {}).get("dateTime", "")
            if not start or not end:
                continue
            try:
                start_dt = datetime.fromisoformat(start)
                end_dt = datetime.fromisoformat(end)
            except Exception:
                continue
            if start_dt <= now <= end_dt:
                return True
        return False
    except Exception as e:
        logger.error("gcal is_event_active_now error: %s", e)
        return False


def create_event(
    title: str,
    start_iso: str,
    end_iso: Optional[str] = None,
    description: Optional[str] = None,
    attendees: Optional[list] = None,
) -> Optional[dict]:
    service = get_calendar_service()
    if not service:
        return None
    tz = ZoneInfo(GOOGLE_TIMEZONE)
    try:
        start_dt = datetime.fromisoformat(start_iso)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=tz)
        start_iso = start_dt.isoformat()
        if end_iso:
            end_dt = datetime.fromisoformat(end_iso)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=tz)
        else:
            end_dt = start_dt + timedelta(hours=1)
        end_iso = end_dt.isoformat()
    except Exception as e:
        logger.error("gcal create_event date parse error: %s", e)
        return None

    body = {
        "summary": title,
        "start": {"dateTime": start_iso, "timeZone": GOOGLE_TIMEZONE},
        "end": {"dateTime": end_iso, "timeZone": GOOGLE_TIMEZONE},
        # Siempre adjuntar un Google Meet al crear el evento.
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    if description:
        body["description"] = description

    clean_attendees = []
    for a in (attendees or []):
        email = (a or "").strip()
        if email and "@" in email:
            clean_attendees.append({"email": email})
    if clean_attendees:
        body["attendees"] = clean_attendees

    try:
        request = service.events().insert(
            calendarId=GOOGLE_CALENDAR_ID,
            body=body,
            conferenceDataVersion=1,
            sendUpdates="all" if clean_attendees else "none",
        )
        return request.execute()
    except HttpError as e:
        logger.error("gcal create error: %s", e)
        return None
    except Exception as e:
        logger.error("gcal create unexpected error: %s", e)
        return None


def _meet_link(event: dict) -> str:
    """Devuelve el enlace de Google Meet del evento, o '' si no tiene."""
    link = event.get("hangoutLink", "")
    if link:
        return link
    for ep in event.get("conferenceData", {}).get("entryPoints", []) or []:
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]
    return ""


def format_event_for_reminder(event: dict, notification_type: str) -> str:
    title = event.get("summary", "(sin titulo)")
    start_str = event.get("start", {}).get("dateTime", "")
    location = event.get("location", "")
    try:
        start_dt = datetime.fromisoformat(start_str)
        time_str = start_dt.strftime("%H:%M")
    except Exception:
        time_str = ""

    minutes = None
    if notification_type.startswith("T-"):
        try:
            minutes = int(notification_type[2:])
        except ValueError:
            minutes = None

    if minutes == 0:
        msg = "\U0001f680 Empieza ahora: *" + title + "*"
    elif minutes is not None and minutes > 0:
        emoji = "\U0001f3c1" if minutes <= 5 else "⏰"
        msg = emoji + " En " + str(minutes) + " min: *" + title + "*"
        if time_str and minutes >= 15:
            msg = msg + " (" + time_str + ")"
    else:
        msg = "Recordatorio: " + title

    if location:
        msg = msg + "\n\U0001f4cd " + location
    meet_link = _meet_link(event)
    if meet_link:
        msg = msg + "\n\U0001f4f9 " + meet_link
    return msg


def format_event_for_daily_preview(event: dict) -> str:
    title = event.get("summary", "(sin titulo)")
    location = event.get("location", "")
    start_str = event.get("start", {}).get("dateTime", "")
    end_str = event.get("end", {}).get("dateTime", "")
    attendees = event.get("attendees", []) or []

    time_str = ""
    try:
        start_dt = datetime.fromisoformat(start_str)
        start_time = start_dt.strftime("%H:%M")
        time_str = start_time
        try:
            end_dt = datetime.fromisoformat(end_str)
            time_str = start_time + "-" + end_dt.strftime("%H:%M")
        except Exception:
            pass
    except Exception:
        pass

    line = "• "
    if time_str:
        line = line + time_str + "  *" + title + "*"
    else:
        line = line + "*" + title + "*"

    if location:
        line = line + "\n   \U0001f4cd " + location

    guest_count = len([a for a in attendees if a.get("email")])
    if guest_count:
        line = line + "\n   \U0001f465 " + str(guest_count) + " invitado" + ("s" if guest_count != 1 else "")
    return line


def format_event_for_creation(event: dict) -> str:
    title = event.get("summary", "(sin titulo)")
    start = event.get("start", {}).get("dateTime", "")
    end = event.get("end", {}).get("dateTime", "")
    link = event.get("htmlLink", "")
    attendees = event.get("attendees", []) or []

    date_str = ""
    start_time = ""
    end_time = ""
    try:
        start_dt = datetime.fromisoformat(start)
        date_str = start_dt.strftime("%Y-%m-%d")
        start_time = start_dt.strftime("%H:%M")
    except Exception:
        pass
    try:
        end_dt = datetime.fromisoformat(end)
        end_time = end_dt.strftime("%H:%M")
    except Exception:
        pass

    msg = "\U0001f4c5 Evento creado: *" + title + "*"
    if date_str:
        msg = msg + "\n\U0001f4c6 " + date_str
    if start_time and end_time:
        msg = msg + " ⏰ " + start_time + " - " + end_time

    guest_emails = [a.get("email", "") for a in attendees if a.get("email")]
    if guest_emails:
        msg = msg + "\n\U0001f465 Invitados: " + ", ".join(guest_emails)

    meet_link = _meet_link(event)
    if meet_link:
        msg = msg + "\n\U0001f4f9 Meet: " + meet_link

    if link:
        msg = msg + "\n\U0001f517 " + link
    return msg
