# TODO

Items fuera del scope inmediato (mayo 2026).

## Calendar — proximas iteraciones

Hecho: listar agenda del dia, editar y cancelar eventos por WhatsApp, y
eventos all-day visibles en el resumen matutino y el preview de mañana.

Pendiente:

- Soporte multi-calendario (personal + Yave + Los Lagos por separado).
  Requiere decidir como se configuran los IDs de calendario (ej. una env
  var `GOOGLE_CALENDAR_IDS` separada por comas) y como se elige a cual va
  cada evento nuevo.
- Al editar un evento, re-sincronizar el `event_followup` asociado (hoy el
  follow-up post-evento sigue agendado segun la hora de fin original).

## Obsidian — siguiente fase tras Calendar

- Restaurar integracion con vault de Obsidian. El codigo fue eliminado en
  commit `703bc31`, recuperable completo en commit `536065b` (incluye
  push_to_vault, sync_vault_to_db, parse_frontmatter, etc.)
- Decidir: restaurar tal cual vs reescribir adaptado al schema Turso actual
- Confirmar estructura de carpetas previa: `proyectos/los-lagos`,
  `proyectos/yave`, `tareas`, `inbox`, `recordatorios`, `contexto`

## Autonomia — fase 2 (ejecutar con confirmacion)

- Intent de busqueda/cotizacion: el asistente investiga en la web y
  entrega un resultado con recomendacion.
- Ejecutar acciones (ej. compras) solo tras confirmacion explicita por
  WhatsApp y con limite de monto.
