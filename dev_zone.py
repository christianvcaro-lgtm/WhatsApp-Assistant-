"""
Sandbox editable via el comando /dev por WhatsApp.

Este es el UNICO archivo que /dev puede modificar. Si necesitas agregar
comandos personalizados (ej. "/mood", "/frase"), pidele a /dev que los
agregue aqui.

Contrato:
- Debe seguir existiendo la funcion handle_custom_command(lower, original)
  que recibe el texto en minusculas y el original, y devuelve un string
  (la respuesta a enviar por WhatsApp) o None si no aplica.
- No importar modulos del proyecto principal (app, gcal, gmail) para evitar
  importaciones circulares.
- Usar stdlib libremente (random, datetime, etc.).
"""

from typing import Optional


def handle_custom_command(lower: str, original: str) -> Optional[str]:
    """Devuelve respuesta para comandos custom, o None si no aplica."""
    return None
