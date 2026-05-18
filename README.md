# 🤖 Asistente Personal WhatsApp

Bot de WhatsApp que captura tareas, ideas, te manda recordatorios y te da visibilidad de todo lo que tienes encima.

## Qué hace

- **Tareas**: Le dices “tengo que llamar a Juan mañana” → la guarda con prioridad y fecha
- **Ideas**: Le dices “idea: hacer webinar de YAVE” → la guarda categorizada
- **Recordatorios**: Le dices “recuérdame a las 3pm revisar métricas” → te avisa a esa hora
- **Calendario**: Agenda, consulta (“qué tengo mañana”), edita (“muévela a las 4pm”) y cancela eventos de Google Calendar por WhatsApp
- **Resumen matutino**: A las 7am te manda resumen del día con tareas urgentes y la agenda del día
- **Preview de mañana**: A las 7pm te lista las reuniones (si tienes Google Calendar conectado), las tareas que vencen al día siguiente y las vencidas que arrastras
- **Cierre nocturno**: A las 9pm te dice qué completaste y qué queda pendiente
- **Consultas**: “pendientes”, “ideas”, “resumen” → te muestra todo al instante

## Comandos rápidos (sin gastar tokens)

|Comando     |Acción              |
|------------|--------------------|
|`pendientes`|Ver todas las tareas|
|`ideas`     |Ver ideas recientes |
|`resumen`   |Resumen del día     |
|`ayuda`     |Ver comandos        |

## Setup paso a paso

### 1. Subir a GitHub

```bash
# Crear repo
git init
git add .
git commit -m "Asistente personal WhatsApp"

# Crear repo en github.com y luego:
git remote add origin https://github.com/TU_USUARIO/whatsapp-assistant.git
git push -u origin main
```

### 2. Deploy en Railway

1. Ve a [railway.app](https://railway.app) y logueate con GitHub
1. Click “New Project” → “Deploy from GitHub repo”
1. Selecciona el repo `whatsapp-assistant`
1. Ve a la pestaña **Variables** y agrega:

**Obligatorias:**

|Variable           |Valor                                          |
|-------------------|-----------------------------------------------|
|`WHATSAPP_TOKEN`   |El token que generaste en Meta (largo)         |
|`WHATSAPP_PHONE_ID`|`653078644555574`                              |
|`VERIFY_TOKEN`     |`mi_asistente_personal_2024` (o el que quieras)|
|`OPENAI_API_KEY`   |Tu API key de OpenAI                           |
|`MY_PHONE_NUMBER`  |Tu número con código país: `573XXXXXXXXX`      |
|`TURSO_URL`        |URL de tu base de datos Turso (libsql)         |
|`TURSO_TOKEN`      |Token de autenticación de Turso                |

**Opcionales:**

|Variable           |Valor                                                    |
|-------------------|---------------------------------------------------------|
|`TIMEZONE`         |Zona horaria del bot (default `America/Bogota`)          |
|`ADMIN_PASSWORD`   |Contraseña del panel `/admin` (sin esto, el panel queda cerrado)|

**Google Calendar / Gmail (opcional — sin esto el bot funciona, solo sin esas features):**

|Variable               |Valor                                              |
|------------------------|---------------------------------------------------|
|`GOOGLE_CLIENT_ID`      |Client ID de tu app OAuth de Google                |
|`GOOGLE_CLIENT_SECRET`  |Client Secret de tu app OAuth de Google            |
|`GOOGLE_REFRESH_TOKEN`  |Refresh token (genéralo con `auth_helper.py`)      |
|`GOOGLE_TIMEZONE`       |Zona horaria del calendario (default `America/Bogota`)|
|`GOOGLE_CALENDAR_ID`    |ID del calendario a usar (default `primary`)       |

**Obsidian (opcional — espejo de tareas/ideas/recordatorios en un vault):**

|Variable        |Valor                                                  |
|-----------------|-------------------------------------------------------|
|`GITHUB_TOKEN`   |Token de GitHub con acceso al repo del vault           |
|`VAULT_REPO`     |Repo del vault, formato `usuario/repo`                 |

1. Railway despliega automáticamente. Copia la URL que te da (ej: `https://tu-app.up.railway.app`)

### 3. Configurar Webhook en Meta

1. Ve a [developers.facebook.com](https://developers.facebook.com) → tu app “Asistente Personal”
1. WhatsApp → Configuración (o API Setup)
1. Busca la sección “Webhook”
1. **Callback URL**: `https://tu-app.up.railway.app/webhook`
1. **Verify Token**: `mi_asistente_personal_2024` (el mismo que pusiste en Railway)
1. Click “Verificar y guardar”
1. Suscríbete al campo **messages**

### 4. Agregar tu número de prueba

En la sección “API Setup” de Meta:

1. En “Para”, agrega tu número personal de WhatsApp
1. Te llegará un código de verificación al WhatsApp
1. Ingrésalo

### 5. Probar

Manda un mensaje al número de prueba (+1 555 141 1988):

- “Tengo que revisar las métricas de Los Lagos”
- “Idea: agregar dashboard de métricas a YAVE”
- “Recuérdame a las 3pm llamar al arquitecto”
- “pendientes”
- “resumen”

## Estructura

```
whatsapp-assistant/
├── app.py              # Núcleo del bot (servidor + lógica + DB + scheduler)
├── gcal.py             # Integración con Google Calendar
├── gmail.py            # Integración con Gmail (filtro de correos)
├── obsidian.py         # Integración con un vault de Obsidian en GitHub
├── auth_helper.py      # Genera el GOOGLE_REFRESH_TOKEN (OAuth)
├── requirements.txt    # Dependencias Python
├── Procfile            # Comando de inicio para Railway
├── TODO.md             # Backlog de mejoras
└── README.md           # Este archivo
```

## Vault de Obsidian (opcional)

Si configuras `GITHUB_TOKEN` y `VAULT_REPO`, el bot escribe cada tarea, idea,
recordatorio y nota de contexto como un archivo markdown en un repo de GitHub
que hace de vault de Obsidian. Al completar o descartar una tarea, actualiza
el `estado:` en su nota.

Para conectarlo:

1. Crea un repo **privado** en GitHub para tu vault (ej: `tu-usuario/obsidian-vault`).
2. En Obsidian, instala el community plugin **Obsidian Git** y apúntalo a ese
   repo. El plugin hace commit/push automático de tus cambios y pull de los del
   bot, así PC y bot comparten las mismas notas.
3. Genera un **Personal Access Token** de GitHub con permiso de escritura sobre
   ese repo y ponlo en `GITHUB_TOKEN`.
4. Pon `usuario/repo` del vault en `VAULT_REPO`.

El bot organiza las notas en carpetas: `tareas`, `inbox`, `recordatorios`,
`contexto`, `proyectos/los-lagos` y `proyectos/yave`.

> La base de datos es **Turso** (libsql) remota, configurada vía `TURSO_URL` y `TURSO_TOKEN`. No hay archivo SQLite local.

## Endpoints auxiliares

Puedes ver tus datos desde el navegador:

- `GET /health` → Status del bot
- `GET /tasks` → Tareas pendientes (JSON)
- `GET /ideas` → Ideas recientes (JSON)
- `GET /summary` → Resumen del día (JSON)
- `GET /context` → Conocimiento personal aprendido (JSON)
- `GET /admin` → Panel para editar el prompt del sistema y los horarios (requiere `ADMIN_PASSWORD`)

## Después de los 90 días

Cuando expire el número de prueba:

1. Compra un chip prepago
1. En Meta → WhatsApp Manager → “Agregar número de teléfono”
1. Registra el nuevo número
1. Actualiza `WHATSAPP_PHONE_ID` en Railway con el nuevo ID
1. Listo

## Costos estimados

- **Railway**: ~$5/mes (gratis los primeros $5)
- **OpenAI API**: ~$1-3/mes (depende de cuánto lo uses)
- **Turso**: gratis en el plan inicial
- **WhatsApp**: Gratis primeros 1000 conversaciones/mes
- **Total**: ~$5-8 USD/mes
