# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Facebook Groups Auto-Poster** — multi-account automation system for Facebook Groups.

- REST API (Flask) orchestrated by **OpenClaw** (external system)
- Browser automation via **Patchright** (patched Chromium) + **Emunium** (OS-level mouse/keyboard)
- SQLite WAL database for accounts, jobs, cookies, and login history
- Admin web panel for account CRUD and publishing
- Job scheduling with webhook callbacks (fire-and-forget)

**Core files in `facebook_auto_poster/`:**
| File | Responsibility |
|------|---------------|
| `config.py` | ENV loading, `AccountConfig` dataclass, fingerprint + timezone helpers |
| `job_store.py` | SQLite schema, all DB operations (WAL mode, no threading.Lock) |
| `facebook_poster_async.py` | Async Patchright login + publish (one instance per account) |
| `account_manager_async.py` | Async orchestration: `asyncio.Semaphore` for parallel account handling |
| `api_server.py` | Flask REST API + admin panel (all routes) |
| `worker_core.py` | Shared job execution logic: account locks, rate-limit filter, AsyncAccountManager, webhook |
| `api_main.py` | Entry point API-only (when `SPLIT_PROCESSES=1` — no scheduler/workers) |
| `worker_main.py` | Entry point Worker-only: orphan recovery, job polling, Chrome pool, graceful shutdown |
| `main.py` | Entry point monolítico (API + scheduler, default) |
| `scheduler_runner.py` | Daemon that polls for scheduled jobs every 30s |
| `logging_config.py` | Central logging setup — text mode or structured JSON (structlog) |
| `crypto.py` | Fernet password encryption/decryption — master key auto-generated in `.secret.key` |
| `proxy_manager.py` | SIM hotspot proxy pool — health checker daemon + `resolve_proxy()` + LRU assignment |
| `metrics.py` | Prometheus metrics export (`METRICS_ENABLED=1`) — counters, histograms, gauges |
| `adaptive_selector.py` | `AdaptivePlaywrightBridge` — 3-level selector recovery: DB approved → Scrapling → Gemini |
| `selector_repair.py` | Gemini fallback for broken selectors — captures HTML, suggests XPath candidates, stores as pending |
| `group_discoverer.py` | Autonomous Facebook group discovery via DOM scraping (no API calls) |
| `text_variation.py` | Gemini paraphrase with SQLite cache + TTL — preserves URLs, numbers, emojis |
| `gemini_commenter.py` | Gemini API integration (human-like comments during warmup) |
| `human_browsing.py` | Async feed warmup before posting (`HumanBrowsingAsync`) |
| `webhook.py` | Async callbacks to OpenClaw |
| `setup_accounts.py` | First-run utility: opens browser for manual CAPTCHA + saves cookies |
| `v2_app.py` | FastAPI wrapper (optional, when `USE_FASTAPI=1`) |
| `v2_router.py` | FastAPI endpoints `/v2/*` (same contract as Flask endpoints) |
| `v2_models.py` | Pydantic models for request/response validation |
| `v2_deps.py` | FastAPI dependencies (API key + rate limit) |

## Running

```bash
pip install -r facebook_auto_poster/requirements.txt
copy facebook_auto_poster\.env.example facebook_auto_poster\.env  # fill credentials
python facebook_auto_poster/main.py
# → http://0.0.0.0:5000  (API + admin panel)
```

## Architecture

### Data Flow

```
OpenClaw → POST /post (X-API-Key)
         → api_server._run_job() [daemon thread]
         → AsyncAccountManager.run() [async, Semaphore-based concurrency]
         → FacebookPosterAsync × N accounts [asyncio.gather]
             ├─ login()                  [cookie restore → email/pass fallback]
             └─ publish_to_all_groups()
         → job_store.mark_done()
         → webhook.fire() [fire-and-forget]

Scheduled jobs: POST /schedule → DB → scheduler_runner (every 30s) → same pipeline
```

**Async design (Fase 3.1):** All posting is now async-first. No sync version remains. `AsyncAccountManager` controls concurrency via `asyncio.Semaphore(max_concurrent)` (default 3, configurable via `EXECUTION_MODE` env var).

### Anti-Detection Stack

- **Patchright**: patched Chromium binary — hides `navigator.webdriver` at binary level
- **Emunium**: OS-level Bézier mouse curves. Used for clicks; typing uses `page.keyboard` (no OS focus dependency)
- **Fingerprints** (`fingerprints.json`): 15 profiles (Chrome 130–132, 6 LATAM+ES locales). One unique fingerprint per account, persisted in DB
- **Per-account identity**: UA, viewport, locale, timezone_id, color_scheme, `sec-ch-ua` headers, `hardwareConcurrency`, `deviceMemory`, `platform` via `add_init_script`
- **Typing**: log-normal delays, 1.5% typo rate with grouped correction (1–3 chars), 2% micro-pauses
- **Active hours**: per-account `(start, end)` tuple + `ZoneInfo` timezone — evaluated at execution time

### Logging (`logging_config.py`)

Centralised logging setup — all modules call `logging.getLogger(name)` as usual; `logging_config` controls the output format.

**Two modes, switched by env var or `CONFIG["structured_logging"]`:**

| Mode | Activate | Output |
|------|----------|--------|
| Text (default) | `STRUCTURED_LOGGING=0` (or unset) | `2026-05-01 12:00:00 - [poster.elena] - INFO - mensaje` |
| JSON | `STRUCTURED_LOGGING=1` | `{"event":"...", "level":"info", "logger":"poster.elena", "account":"elena", "timestamp":"..."}` |

**Public API:**
```python
from logging_config import setup_logging, bind_account, unbind_account, get_formatter

setup_logging(structured=True, log_dir=Path("logs"))   # call once at startup (main.py)
bind_account("elena")     # inject account field into every log from this thread
unbind_account()          # clear thread-local context (called in FacebookPosterAsync.close())
get_formatter()           # returns the active formatter — used by per-account file handlers
```

**Key design decisions (gotchas):**
- `bind_account` is called inside `login()`, not `__init__`, because `__init__` runs in the calling thread while `login()` runs in the worker thread — `structlog.contextvars` are thread-local.
- Per-account `FileHandler` sets `logger.propagate = False` to prevent duplicate lines in the root handler.
- `bind_account` / `unbind_account` are no-ops when `structured_logging=False` — safe to call unconditionally.
- On Windows, `STRUCTURED_LOGGING=1` requires `structlog~=24.0` (already in `requirements.txt`). Tests require `tzdata~=2024.0` (in `requirements-dev.txt`) because Windows Python has no built-in IANA timezone database.

### Database Schema (SQLite WAL — no Python lock)

| Table | Purpose |
|-------|---------|
| `accounts` | PK: name. Fields: email (login ID: email or phone), groups (JSON), timezone, active_hours, fingerprint_json, password_enc, is_active |
| `account_cookies` | PK: email. Serialized session cookies (plaintext JSON — see known issues) |
| `jobs` | Queue: immediate + scheduled. Status: pending/running/done/failed/cancelled |
| `job_results` | Per-account, per-group success/failure with group_tag snapshot |
| `login_events` | Login audit trail |
| `gemini_usage` | Daily Gemini API quota tracking per account |
| `group_tags` | Human-readable emoji-safe labels per group ID |
| `account_bans` | Ban cooldown tracking per account (48h cooldown, auto-deactivation) |
| `rate_limit_events` | SQLite-backed rate limiter (survives restarts) — replaces in-memory dict |
| `text_variations` | Gemini paraphrase cache with TTL — keyed by (account, group, text_hash) |
| `discovery_runs` | Audit log of group discovery attempts (status: running/done/failed) |
| `discovered_groups` | Catalog of groups found via discovery — `added_to_posting` flag for admin approval |
| `proxy_nodes` | SIM hotspot nodes: IP, port, protocol, status, last health check |
| `account_proxy_assignment` | Account ↔ proxy node mapping with LRU `last_used_at` |
| `selector_repairs` | Gemini-suggested XPath candidates (status: pending/approved/rejected) |
| `templates` | Reusable post templates (title, text, image) managed via admin panel |

### Login Identifiers (Email or Phone)

Cada cuenta de Facebook se autentica usando **uno** de estos identificadores:
- **Email**: `user@dominio.com` (valida el `@` y TLD)
- **Teléfono**: `+521234567890` o `521234567890` (7–15 dígitos, `+` opcional, internacional)

El campo `email` en la BD almacena **indistintamente** email o teléfono. Facebook's login form `input[name='email']` acepta ambos.

**Configuración (tres formas):**
1. **Panel admin** (`/admin`): campo "Email o teléfono" en el modal de cuentas (recomendado)
2. **`.env` fallback**: variables `{PREFIX}_EMAIL` o `{PREFIX}_PHONE` en el primer arranque
3. **BD directo**: `UPDATE accounts SET email='...' WHERE name=...`

Cookies se asocian por identificador (se buscan en `account_cookies.email` usando cualquiera de los dos formatos).

### Grupos (Grupos de Publicación) — Opcional

Los IDs de grupos de Facebook son **opcionales** al crear/editar una cuenta:
- Una cuenta sin grupos **no publicará** (se salta silenciosamente al cargar cuentas)
- **Panel admin**: badge naranja `⚠ Sin grupos` para identificarlas visualmente
- **Logs**: `WARNING [config] Cuenta 'X' omitida — no tiene grupos configurados` cuando se cargan cuentas sin grupos
- **Agregar después**: editar la cuenta en `/admin` y completar el campo "IDs de grupos"
- **`.env` fallback**: `{PREFIX}_GROUPS` es opcional (puede dejarse vacío)

## Authentication & Security

| Layer | Method |
|-------|--------|
| OpenClaw API | `X-API-Key` header, `secrets.compare_digest()` |
| Admin panel | Flask session cookie, signed with `ADMIN_KEY` |
| Rate limiting | 10 req/60s per IP (in-memory, resets on restart) |
| Input validation | Account names `[a-z0-9_]{1,30}`, login IDs (email or phone `+?[0-9]{7,15}`), groups digits-only |
| Image uploads | Extension whitelist, MIME check, UUID rename, path traversal prevention |
| SQL injection | All queries use `?` parameterized statements |

## API Endpoints

### Public (no auth)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Basic health check |

### OpenClaw (X-API-Key required)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/accounts` | List active accounts + groups |
| POST | `/post` | Immediate publication (JSON or multipart) |
| POST | `/schedule` | Schedule publication (ISO 8601 `scheduled_for`) |
| GET | `/schedule` | List pending scheduled jobs |
| DELETE | `/schedule/<id>` | Cancel scheduled job |
| PUT | `/groups/<id>/tag` | Set group tag |
| GET | `/health/detailed` | Health + worker/DB/scheduler status |
| GET | `/metrics` | Prometheus metrics (`METRICS_ENABLED=1`) |

### Admin (session cookie required)
| Method | Path | Description |
|--------|------|-------------|
| GET/POST | `/admin/login` | Login with ADMIN_KEY |
| GET | `/admin/logout` | Logout |
| GET | `/admin` | Admin SPA |
| GET | `/admin/publish` | Publishing interface |
| GET/POST/PUT/DELETE | `/admin/api/accounts[/<name>]` | Account CRUD |
| POST | `/admin/api/accounts/<name>/password` | Set/clear custom password |
| POST | `/admin/api/accounts/<name>/login` | Trigger manual login |
| GET | `/admin/api/accounts/<name>/login/<run_id>` | Poll manual login status |
| POST | `/admin/api/accounts/<name>/proxy` | Assign proxy to account |
| DELETE | `/admin/api/accounts/<name>/proxy` | Remove proxy assignment |
| POST | `/admin/api/accounts/<name>/discover-groups` | Trigger group discovery |
| GET | `/admin/api/discovery/<run_id>` | Poll discovery status |
| GET | `/admin/api/accounts/<name>/discovered-groups` | List discovered groups |
| POST | `/admin/api/accounts/<name>/discovered-groups/<id>/add` | Approve + add group |
| GET/PUT | `/admin/api/groups[/<id>/tag]` | List groups / set tag |
| GET | `/admin/api/history` | Login events |
| GET | `/admin/api/jobs` | Recent jobs |
| GET | `/admin/api/queue` | Live queue status (jobs_by_status, accounts_in_progress) |
| GET | `/admin/api/bans` | Active ban cooldowns |
| POST | `/admin/api/bans/<name>/clear` | Clear ban for account |
| GET | `/admin/api/selector-repairs` | List pending DOM repair candidates |
| POST | `/admin/api/selector-repairs/<id>/approve` | Approve selector repair |
| POST | `/admin/api/selector-repairs/<id>/reject` | Reject selector repair |
| GET/POST | `/admin/api/proxies[/<node_id>]` | List / add proxy nodes |
| DELETE | `/admin/api/proxies/<node_id>` | Remove proxy node |
| PUT | `/admin/api/proxies/<node_id>/status` | Enable/disable proxy node |
| GET/POST/PUT/DELETE | `/admin/api/templates[/<id>]` | Template CRUD |
| POST | `/admin/api/upload-images` | Upload images for publishing |
| POST/GET/DELETE | `/admin/api/schedule[/<id>]` | Schedule jobs from admin panel |

### FastAPI `/v2` (X-API-Key required, same contract as Flask)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/v2/accounts` | List active accounts |
| POST | `/v2/post` | Immediate publication (Pydantic validated) |
| POST | `/v2/schedule` | Schedule publication |
| GET | `/v2/schedule` | List pending scheduled jobs |
| DELETE | `/v2/schedule/{id}` | Cancel scheduled job |

## Configuration (config.py `CONFIG` dict)

Key behavioral parameters:
- `wait_between_groups_min/max` — 30–60s between group posts
- `wait_after_login_min/max` — 5–10s after login
- `wait_between_accounts_min/max` — 60–120s between accounts
- `max_groups_per_session` — 5 groups per account per run
- `execution_mode` — `"sequential"` (default) or `"parallel"` (multiprocessing)
- `human_browsing_enabled` — warmup feed scroll before posting (60% probability)
- `gemini_comment_enabled` — AI comments during warmup (20% probability, max 2/session)

Hour guard: accounts have `active_hours=(7,23)` + `timezone="UTC"` in DB. `AccountManager.run()` skips accounts outside their local window; fails only if **all** are out of window.

## Account Loading Priority

1. **SQLite DB** (`job_store.list_accounts_full()`) — managed via admin panel
2. **`.env` fallback** — first run before DB is populated (`ACCOUNT_NAMES`, `{PREFIX}_EMAIL` o `{PREFIX}_PHONE`, `{PREFIX}_GROUPS`)

Shared password: `FB_PASSWORD` in `.env` used for all accounts (see known issues).

## FacebookPosterAsync Lifecycle (Async-only)

```python
poster = FacebookPosterAsync(account, config)    # opens Chromium, loads fingerprint
if await poster.login():                         # cookie restore → email/pass fallback
    results = await poster.publish_to_all_groups(text, image_path=image_path)
await poster.close()
```

**Note:** All poster operations are now async. Called via `asyncio.gather()` in `AsyncAccountManager.run()`.

## Development

```bash
# Quick test run
python facebook_auto_poster/test_run.py

# View DB
sqlite3 facebook_auto_poster/jobs.db "SELECT name, email FROM accounts WHERE is_active=1;"
sqlite3 facebook_auto_poster/jobs.db "SELECT id, status FROM jobs ORDER BY created_at DESC LIMIT 10;"
```

**Debug tips:**
- Logs: `logs/main.log` + `logs/{account}.log`
- Screenshots on failures: `screenshots/{account}/`
- Bad cookies: just delete the account's cookie row in DB — next login will re-authenticate

**Git branch naming:** `fase3.X-nombre` — git no permite `fase-3/3.X` cuando `fase-3` ya existe como rama (punto como separador, no slash).

**Tests ≠ validación de producción:** los tests cubren lógica interna (horarios, CRUD, concurrencia). No cubren que Facebook acepta la publicación hoy, que los XPaths funcionan, ni que el anti-detección pasa los filtros actuales. Después de cualquier cambio grande, hacer una publicación de prueba manual con cuenta real antes de escalar.

**Escala de proxies:** activar proxy por cuenta (`proxy_manager.py`) **antes de escalar a más de 3 cuentas**. Sin IP por cuenta, Facebook puede correlacionar el cluster y banear todo de golpe.

## Known Issues & Plan

See [plan/00_CONTEXTO.md](plan/00_CONTEXTO.md) for active risks, phase status, and improvement tracking.

Active critical risks: no per-account proxy (cluster-ban), session cookies unencrypted in SQLite.

## Session Rules

### Reglas estrictas (NO negociables)

1. **Solo implementar lo pedido explícitamente** — si se encuentra algo "mejorable" fuera del alcance, reportarlo como nota al final, no implementarlo.
2. **Antes de crear un archivo nuevo**, confirmar que no existe ya uno similar.
3. **Antes de modificar un archivo existente**, leer su contenido completo.
4. **No refactorizar** código que no esté en el alcance de la tarea.
5. **No agregar** imports, dependencias o funciones auxiliares no solicitadas.
6. **Si una tarea requiere tocar más archivos de los esperados**, pausar y reportar antes de continuar.
7. **Ser crítico con el código de otras ramas** — no asumir que es correcto.

### Filosofía de cambio

1. **Mapear el impacto** antes de tocar código o documentación — identificar todos los consumidores del código; leer el código real antes de documentarlo (nunca documentar de memoria o inferencia).
2. **Cambiar de adentro hacia afuera** — primero DB/modelo, luego backend, luego UI.
3. **Un cambio por commit** — cada bloque en su propio commit. Facilita `git revert` quirúrgico.
4. **Verificar el contrato de API antes y después** — anotar qué devuelve cada función actualmente y qué se espera.
5. **Probar el flujo completo** — si se cambia una función, verificar que los otros consumidores no se rompieron.

### Protocolo para implementaciones grandes

Para cualquier tarea que toque más de 2 archivos o expanda la arquitectura existente:

1. **Declarar scope antes de empezar** — listar explícitamente:
   - **Whitelist**: archivos que se van a modificar
   - **Blacklist**: archivos protegidos que no se tocan sin confirmación explícita del usuario
   - **Read-only**: archivos que se pueden leer como referencia pero no modificar

2. **Clasificar riesgo por bloque** — cada bloque de trabajo lleva etiqueta: `NULO / BAJO / MEDIO / ALTO`. Los bloques de riesgo ALTO van en sesión separada.

3. **Documentar contrato antes de tocar** — para cada función que se va a cambiar, anotar en texto:
   - Qué devuelve/hace hoy
   - Qué se espera que devuelva/haga después
   - Qué consumidores se ven afectados

4. **Commit al terminar cada bloque** — no acumular varios bloques sin commit. El usuario revisa antes de continuar al siguiente.

5. **Parar ante sorpresas** — si al implementar un bloque aparece que hay que tocar un archivo fuera del scope declarado, parar, reportar qué se encontró y por qué es necesario, y esperar confirmación antes de continuar.

## For Next Session

```bash
git log --oneline -10
sqlite3 facebook_auto_poster/jobs.db "SELECT id, status FROM jobs WHERE status='pending';"
tail -50 facebook_auto_poster/logs/main.log
```
