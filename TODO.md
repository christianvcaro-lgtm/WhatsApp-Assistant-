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

Hecho: integracion de ESCRITURA restaurada en el modulo `obsidian.py`. El bot
escribe cada tarea/idea/recordatorio/contexto como nota markdown en un repo de
GitHub que hace de vault, y actualiza el `estado:` al completar o descartar
tareas. Se activa con las env vars `GITHUB_TOKEN` y `VAULT_REPO`.

Pendiente — lado de LECTURA:

- El `sync_vault_to_db` viejo (importar notas del vault a la DB en cada
  arranque) NO se restauro: con la DB persistente de Turso su dedup por
  titulo+fecha genera duplicados (la fecha local del frontmatter no coincide
  con el `created_at` en UTC). Necesita rediseno.
- Opcion mas liviana propuesta: en vez de auto-importar todo, un intent para
  consultar el vault bajo demanda ("busca en mis notas X"), sin volcar nada a
  las tablas. Evita duplicados y no carga el vault entero en el prompt.

## Autonomia — fase 2 (ejecutar con confirmacion)

- Intent de busqueda/cotizacion: el asistente investiga en la web y
  entrega un resultado con recomendacion.
- Ejecutar acciones (ej. compras) solo tras confirmacion explicita por
  WhatsApp y con limite de monto.
