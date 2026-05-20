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

## Obsidian

Integracion bidireccional con un vault de Obsidian alojado en GitHub, en
`obsidian.py`. Se activa con las env vars `GITHUB_TOKEN` y `VAULT_REPO`.

Escritura: el bot guarda cada tarea, idea, recordatorio y nota de contexto
como nota markdown con frontmatter, y actualiza el `estado:` al completar o
descartar una tarea.

Lectura: intent `vault_search` para buscar bajo demanda en las notas del
vault ("busca en mis notas X", "que tengo escrito sobre Y") usando GitHub
code search. No se auto-importa nada a la DB — eso evita duplicados sobre
la DB persistente de Turso y no carga el vault entero en cada mensaje.

## Autonomia — fase 2 (ejecutar con confirmacion)

- Intent de busqueda/cotizacion: el asistente investiga en la web y
  entrega un resultado con recomendacion.
- Ejecutar acciones (ej. compras) solo tras confirmacion explicita por
  WhatsApp y con limite de monto.
