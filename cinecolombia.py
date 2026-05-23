"""
cinecolombia.py - Cartelera y deep links de checkout para Cine Colombia (Cartagena).

Habla directo con la API de Vista Omnia (digital-api.cinecolombia.com). El JWT
se refresca automaticamente con Playwright headless (la home tiene Cloudflare,
no se puede curl). El token se cachea por TOKEN_TTL_MIN minutos.

Setup Railway/local:
    pip install -r requirements.txt
    playwright install chromium

Uso como script:
    python cinecolombia.py
"""
import os
import time
import uuid
import asyncio
import logging
import httpx
from datetime import datetime
from typing import Optional, List, Union

logger = logging.getLogger(__name__)

API_BASE = "https://digital-api.cinecolombia.com"
HOME_URL = "https://www.cinecolombia.com/films/"
CHECKOUT_BASE = "https://multiplex.cinecolombia.com/order/showtimes"

CARTAGENA_SITES = {
    "6401": "Caribe Plaza",
    "6601": "Paseo La Castellana",
    "6605": "Plaza Bocagrande",
}

ATTRIBUTE_NAMES = {
    "0000000004": "DOB",
    "0000000007": "SUB",
    "0000000008": "2D",
}

TOKEN_TTL_MIN = 10
_cached_token = ""
_cached_at = 0.0
_token_lock = asyncio.Lock()


def is_configured() -> bool:
    return True


async def _refresh_token() -> str:
    """Lanza Chromium headless, va a la home y extrae window.initialData.api.authToken."""
    from playwright.async_api import async_playwright
    logger.info("cineco: refrescando JWT via Playwright")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
                locale="es-CO",
            )
            page = await context.new_page()
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
            token = await page.evaluate(
                "() => window.initialData && window.initialData.api && window.initialData.api.authToken"
            )
            if not token:
                logger.error("cineco: window.initialData.api.authToken vacio")
                return ""
            return token
        finally:
            await browser.close()


async def get_token() -> str:
    global _cached_token, _cached_at
    if _cached_token and (time.time() - _cached_at) < TOKEN_TTL_MIN * 60:
        return _cached_token
    async with _token_lock:
        if _cached_token and (time.time() - _cached_at) < TOKEN_TTL_MIN * 60:
            return _cached_token
        token = await _refresh_token()
        if token:
            _cached_token = token
            _cached_at = time.time()
        return token


async def _get(path: str, params: Optional[Union[list, dict]] = None):
    token = await get_token()
    if not token:
        return None
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "correlationid": "wa-" + uuid.uuid4().hex[:24],
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(API_BASE + path, headers=headers, params=params)
    if r.status_code == 401:
        logger.info("cineco 401, forzando refresh del JWT")
        global _cached_token
        _cached_token = ""
        token = await get_token()
        if not token:
            return None
        headers["Authorization"] = "Bearer " + token
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(API_BASE + path, headers=headers, params=params)
    if r.status_code != 200:
        logger.error("cineco %s -> %d: %s", path, r.status_code, r.text[:200])
        return None
    try:
        return r.json()
    except Exception as e:
        logger.error("cineco json parse: %s", e)
        return None


def film_title_es(film: dict) -> str:
    title = film.get("title", {})
    for t in title.get("translations", []):
        if t.get("languageTag") == "en":
            return t.get("text", "")
    return title.get("text", "")


async def list_films_with_showtimes_today(business_date: Optional[str] = None) -> list:
    """Devuelve solo peliculas que TIENEN showtimes hoy en los 3 cines de Cartagena.
    Cada item: {id, title, showtimes: [...]}. Esto es lo unico que el bot necesita
    mostrar como cartelera real."""
    if business_date is None:
        business_date = datetime.now().date().isoformat()
    films_data = await _get("/ocapi/v1/films")
    if not films_data:
        return []
    all_films = films_data.get("films", []) if isinstance(films_data, dict) else []
    site_params = [("siteIds", s) for s in CARTAGENA_SITES.keys()]
    out = []
    for film in all_films:
        film_id = film.get("id", "")
        if not film_id:
            continue
        params = [("filmIds", film_id)] + site_params
        sd = await _get("/ocapi/v1/showtimes/by-business-date/" + business_date, params=params)
        showtimes = sd.get("showtimes", []) if isinstance(sd, dict) else []
        if showtimes:
            out.append({
                "id": film_id,
                "title": film_title_es(film),
                "showtimes": showtimes,
            })
    return out


async def get_showtimes(film_id: str, business_date: str) -> list:
    params = [("filmIds", film_id)] + [("siteIds", s) for s in CARTAGENA_SITES.keys()]
    data = await _get("/ocapi/v1/showtimes/by-business-date/" + business_date, params=params)
    if not data:
        return []
    return data.get("showtimes", []) if isinstance(data, dict) else []


def build_checkout_url(showtime_id: str) -> str:
    return CHECKOUT_BASE + "/" + showtime_id + "/seats"


def format_time(showtime: dict) -> str:
    starts = showtime.get("schedule", {}).get("startsAt", "")
    try:
        dt = datetime.fromisoformat(starts)
        return dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return starts[11:16]


def format_attrs(showtime: dict) -> str:
    attrs = showtime.get("attributeIds", []) or []
    labels = [ATTRIBUTE_NAMES.get(a, "") for a in attrs]
    labels = [l for l in labels if l]
    return "/".join(labels) if labels else ""


def site_name(site_id: str) -> str:
    return CARTAGENA_SITES.get(site_id, site_id)


if __name__ == "__main__":
    async def main():
        print("Cartelera Cine Colombia (Cartagena) - hoy:")
        films = await list_films_with_showtimes_today()
        if not films:
            print("Sin peliculas o fallo de auth.")
            return
        for i, f in enumerate(films, 1):
            print(str(i) + ". " + f["title"] + " (" + str(len(f["showtimes"])) + " funciones)")
            for s in f["showtimes"][:3]:
                print("   - " + format_time(s) + " " + site_name(s.get("siteId", "")) +
                      " (" + (format_attrs(s) or "?") + ") -> " + build_checkout_url(s.get("id", "")))
    asyncio.run(main())
