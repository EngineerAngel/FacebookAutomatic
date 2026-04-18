# Facebook Groups Auto-Poster — Resumen Técnico

## Estado del proyecto: ✅ COMPLETO

API HTTP local orquestada por un sistema externo (OpenClaw). Soporta
**publicación inmediata** y **publicaciones agendadas** en paralelo.

---

## Estructura de archivos

```
facebook_auto_poster/
├── .env.example
├── .gitignore
├── requirements.txt          selenium + dotenv + webdriver-manager + flask
├── config.py                 CONFIG dict + AccountConfig + load_accounts()
├── facebook_poster.py        Selenium: login, navegación, publicación
├── account_manager.py        Orquestador: secuencial / paralelo
├── api_server.py             Servidor Flask — 5 endpoints
├── scheduler_store.py        Persistencia JSON thread-safe
├── scheduler_runner.py       Hilo daemon que dispara agendadas
├── main.py                   Entry point (arranca API + scheduler)
├── setup_accounts.py         Login inicial + guardado de cookies
├── test_run.py               Prueba manual desde consola
├── uploaded_images/          Imágenes subidas por multipart (gitignored)
├── schedules.json            Jobs pendientes (gitignored)
└── logs/                     (se crea en runtime)
```

---

## Arquitectura

```
                         ┌──────────────┐
                         │   OpenClaw   │
                         └──────┬───────┘
                                │ HTTP
                     ┌──────────┴──────────┐
                     ▼                     ▼
            ┌────────────────┐    ┌────────────────┐
            │  POST /post    │    │ POST /schedule │
            │  (inmediato)   │    │  (agendado)    │
            └────────┬───────┘    └────────┬───────┘
                     │                     │
                     │                     ▼
                     │           scheduler_store.json
                     │                     │
                     │           scheduler_runner (30s poll)
                     │                     │
                     └──────────┬──────────┘
                                ▼
                      threading.Thread (daemon)
                                │
                                ▼
                         AccountManager
                                │
                                ▼
                        FacebookPoster
                      (login → publicar)
```

Ambas rutas (/post y /schedule) desembocan en el mismo pipeline. El scheduler
runner actúa como un cliente interno que inyecta jobs en la misma cola.

---

## Endpoints

### `GET /accounts`

Lista cuentas configuradas en `.env`. Formato estructurado sin ambigüedad
entre nombre de cuenta e IDs de grupo.

```json
{
  "accounts": [
    {"name": "maria", "groups": ["111", "222", "333"]},
    {"name": "zofia", "groups": ["444", "555", "666"]}
  ]
}
```

### `POST /post` — publicación inmediata

Acepta **JSON** o **multipart/form-data**.

**JSON:**
```json
{
  "text": "Texto del anuncio",
  "image_path": "/ruta/local/img.jpg",
  "accounts": ["maria", "zofia"]
}
```

**Multipart:**
```
text=Texto del anuncio
image=@./local.jpg
accounts=maria,zofia
```

| Campo | Tipo | Obligatorio | Descripción |
|-------|------|-------------|-------------|
| `text` | str | ✅ | Texto a publicar |
| `image_path` | str | ❌ | Ruta en el servidor (JSON only) |
| `image` | file | ❌ | Imagen subida (multipart only) |
| `accounts` | list/CSV | ❌ | Filtro; omitir = todas |

**Respuestas:**
| HTTP | Caso |
|------|------|
| `202` | Aceptado, Selenium corriendo en background |
| `400` | `text` vacío o filtro de cuentas sin coincidencia |
| `403` | Fuera del horario permitido (6:00-22:59) |
| `500` | Error leyendo `.env` |

### `POST /schedule` — agendar

Mismos campos que `/post` + `scheduled_for` obligatorio (ISO 8601).

```json
{
  "text": "Anuncio futuro",
  "scheduled_for": "2026-04-18T15:30:00",
  "accounts": ["maria"]
}
```

**Validaciones:**
- `scheduled_for` debe ser futura
- Hora debe estar en el rango permitido (6:00-22:59)

**Respuesta 201:**
```json
{
  "id": "a3f9c1e8d240",
  "scheduled_for": "2026-04-18T15:30:00",
  "status": "scheduled"
}
```

### `GET /schedule` — listar pendientes

```json
{
  "pending": [
    {"id": "...", "text": "...", "scheduled_for": "...", "accounts": [...]}
  ],
  "count": 1
}
```

### `DELETE /schedule/<id>` — cancelar

- `204` si existía y se canceló
- `404` si no existe

---

## Guardia horaria

```python
post_hours_allowed = range(6, 23)   # 6:00 a 22:59
```

- `/post` verifica la hora **actual** → 403 si está fuera de rango
- `/schedule` verifica la hora **agendada** → 400 si está fuera de rango
- Mensaje que recibe OpenClaw incluye el rango permitido y la hora rechazada

---

## Stack tecnológico

| Capa | Tecnología |
|------|-----------|
| Automatización web | Selenium 4 + ChromeDriver |
| Credenciales | python-dotenv |
| Concurrencia | multiprocessing + threading |
| API HTTP | Flask >= 3 |
| Scheduling | scheduler_store (JSON) + scheduler_runner (hilo daemon) |
| Logging | stdlib logging |
| Anti-detección | CDP + UA personalizado |

---

## Variables .env

```ini
ACCOUNT_NAMES=maria,zofia,elena

MARIA_EMAIL=...
MARIA_PASSWORD=...
MARIA_GROUPS=111,222,333
# ... (repetir por cuenta)

CHROMEDRIVER_PATH=           # opcional
CHROME_PROFILE_PATH=         # opcional, evita CAPTCHAs
CHROME_PROFILE_NAME=Default

EXECUTION_MODE=sequential    # o "parallel"
API_PORT=5000
POST_TEXT=                   # opcional (para test_run.py)
```

---

## Primeros pasos

```bash
pip install -r requirements.txt

copy .env.example .env
# Editar .env con credenciales

python setup_accounts.py     # login inicial por cuenta

python main.py
# → "Facebook Auto-Poster arrancando — API 0.0.0.0:5000 | scheduler activo"
```

---

## Pruebas con curl

```bash
# Ver inventario
curl http://localhost:5000/accounts

# Inmediato — JSON
curl -X POST http://localhost:5000/post \
  -H "Content-Type: application/json" \
  -d '{"text": "Hola"}'

# Inmediato — multipart con imagen
curl -X POST http://localhost:5000/post \
  -F "text=Hola con foto" \
  -F "image=@./promo.jpg" \
  -F "accounts=maria,zofia"

# Agendar
curl -X POST http://localhost:5000/schedule \
  -H "Content-Type: application/json" \
  -d '{"text":"En una hora","scheduled_for":"2026-04-18T15:30:00"}'

# Listar agendadas
curl http://localhost:5000/schedule

# Cancelar
curl -X DELETE http://localhost:5000/schedule/a3f9c1e8d240
```

---

## Tiempos configurables (config.py)

| Parámetro | Valor default |
|-----------|--------------|
| `wait_between_groups_min/max` | 30 – 60 s |
| `wait_after_login_min/max` | 5 – 10 s |
| `wait_between_accounts_min/max` | 60 – 120 s |
| `max_groups_per_session` | 5 |
| `max_retries` | 3 |
| `post_hours_allowed` | 6 – 22 h |
| `browser_headless` | False |
| `browser_window_size` | 1280 × 720 |
| `POLL_SECONDS` (scheduler) | 30 s (en scheduler_runner.py) |
