# 🤖 Asistente Personal WhatsApp

Bot de WhatsApp que captura tareas, ideas, te manda recordatorios y te da visibilidad de todo lo que tienes encima.

## Qué hace

- **Tareas**: Le dices “tengo que llamar a Juan mañana” → la guarda con prioridad y fecha
- **Ideas**: Le dices “idea: hacer webinar de YAVE” → la guarda categorizada
- **Recordatorios**: Le dices “recuérdame a las 3pm revisar métricas” → te avisa a esa hora
- **Resumen matutino**: A las 7am te manda resumen del día con tareas urgentes
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

|Variable           |Valor                                          |
|-------------------|-----------------------------------------------|
|`WHATSAPP_TOKEN`   |El token que generaste en Meta (largo)         |
|`WHATSAPP_PHONE_ID`|`653078644555574`                              |
|`VERIFY_TOKEN`     |`mi_asistente_personal_2024` (o el que quieras)|
|`ANTHROPIC_API_KEY`|Tu API key de Anthropic                        |
|`MY_PHONE_NUMBER`  |Tu número con código país: `573XXXXXXXXX`      |
|`TIMEZONE`         |`America/Bogota`                               |

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
├── app.py              # Todo el bot (servidor + lógica + DB)
├── requirements.txt    # Dependencias Python
├── Procfile           # Comando de inicio para Railway
├── assistant.db       # SQLite (se crea automático)
└── README.md          # Este archivo
```

## Endpoints auxiliares

Puedes ver tus datos desde el navegador:

- `GET /health` → Status del bot
- `GET /tasks` → Tareas pendientes (JSON)
- `GET /ideas` → Ideas recientes (JSON)
- `GET /summary` → Resumen del día (JSON)

## Después de los 90 días

Cuando expire el número de prueba:

1. Compra un chip prepago
1. En Meta → WhatsApp Manager → “Agregar número de teléfono”
1. Registra el nuevo número
1. Actualiza `WHATSAPP_PHONE_ID` en Railway con el nuevo ID
1. Listo

## Costos estimados

- **Railway**: ~$5/mes (gratis los primeros $5)
- **Claude API**: ~$1-3/mes (depende de cuánto lo uses)
- **WhatsApp**: Gratis primeros 1000 conversaciones/mes
- **Total**: ~$5-8 USD/mes
