# TODO

Items fuera del scope de la integracion inicial de Google Calendar (mayo 2026).

## Calendar — proximas iteraciones

- Editar eventos via WhatsApp ("muevelo a las 4pm")
- Cancelar eventos via WhatsApp
- Listar eventos del dia ("que tengo hoy")
- Soporte multi-calendario (personal + Yave + Los Lagos por separado)
- Eventos all-day con aviso por la manana (hoy se ignoran)

## Obsidian — siguiente fase tras Calendar

- Restaurar integracion con vault de Obsidian. El codigo fue eliminado en
  commit `703bc31`, recuperable completo en commit `536065b` (incluye
  push_to_vault, sync_vault_to_db, parse_frontmatter, etc.)
- Decidir: restaurar tal cual vs reescribir adaptado al schema Turso actual
- Confirmar estructura de carpetas previa: `proyectos/los-lagos`,
  `proyectos/yave`, `tareas`, `inbox`, `recordatorios`, `contexto`

## Higiene del repo

- Actualizar README.md (todavia menciona ANTHROPIC_API_KEY, ya migrado a OpenAI)
- Documentar variables de entorno completas en README
