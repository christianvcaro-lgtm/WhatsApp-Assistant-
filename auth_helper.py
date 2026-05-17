"""
auth_helper.py - Genera el refresh token de Google (Calendar + Gmail).

Script de uso unico. Corre una sola vez para obtener el GOOGLE_REFRESH_TOKEN
que despues vive en Railway como variable de entorno. El token resultante
cubre Calendar y lectura de Gmail con un solo valor: reemplaza al token
viejo (solo-Calendar) sin romper nada, porque incluye ambos permisos.

Uso:
    export GOOGLE_CLIENT_ID="..."
    export GOOGLE_CLIENT_SECRET="..."
    python auth_helper.py
"""
import os
import sys

# Google a veces devuelve los scopes en distinto orden al pedirlos; sin esto
# oauthlib aborta con "Scope has changed" al re-autorizar con scopes nuevos.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("ERROR: falta google-auth-oauthlib. Instala con:")
    print("  pip install google-auth-oauthlib google-auth google-api-python-client")
    sys.exit(1)


SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def main():
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print("ERROR: Faltan variables de entorno.")
        print("Antes de correr este script, exporta en tu terminal:")
        print('  export GOOGLE_CLIENT_ID="..."')
        print('  export GOOGLE_CLIENT_SECRET="..."')
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    print("=" * 60)
    print("Auth helper - Google Calendar + Gmail")
    print("=" * 60)
    print()
    print("Voy a abrir tu navegador. Hace lo siguiente:")
    print("  1. Loguea con tu cuenta christianvcaro@gmail.com")
    print("  2. Si Google muestra 'esta app no esta verificada':")
    print("     - Click 'Configuracion avanzada' (o 'Advanced')")
    print("     - Click 'Ir a WhatsApp Assistant (no seguro)'")
    print("     Eso es esperado porque la app esta publicada pero no verificada.")
    print("  3. Acepta los permisos (calendario + lectura de Gmail)")
    print("  4. Vuelves aca, vas a ver el refresh_token impreso")
    print()
    input("Enter para abrir el navegador...")

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
        open_browser=True,
    )

    if not creds.refresh_token:
        print()
        print("ERROR: No se obtuvo refresh_token.")
        print("Esto pasa si ya autorizaste antes sin forzar consent.")
        print("Solucion: ve a https://myaccount.google.com/permissions")
        print("revoca el acceso de WhatsApp Assistant, y vuelve a correr el script.")
        sys.exit(1)

    print()
    print("=" * 60)
    print("AUTH EXITOSO")
    print("=" * 60)
    print()
    print("REFRESH TOKEN (copialo y pegalo en Railway):")
    print()
    print(creds.refresh_token)
    print()
    print("=" * 60)
    print()
    print("En Railway, agrega estas variables de entorno:")
    print(f"  GOOGLE_CLIENT_ID     = {client_id}")
    print("  GOOGLE_CLIENT_SECRET = (el que ya tienes)")
    print("  GOOGLE_REFRESH_TOKEN = (el de arriba)")
    print()
    print("Opcionales (con defaults razonables):")
    print("  GOOGLE_TIMEZONE      = America/Bogota")
    print("  GOOGLE_CALENDAR_ID   = primary")
    print()


if __name__ == "__main__":
    main()
