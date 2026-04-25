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
| `job_store.py` | SQLite schema, all DB operations |
| `facebook_poster.py` | Patchright login + publish (one instance per account) |
| `account_manager.py` | Orchestrates sequential / parallel sessions |
| `api_server.py` | Flask REST API + admin panel |
| `scheduler_runner.py` | Daemon that polls for scheduled jobs every 30s |
| `main.py` | Entry point (API + scheduler) |
| `gemini_commenter.py` | Gemini API integration (human-like comments during warmup) |
| `human_browsing.py` | Feed warmup before posting |
| `webhook.py` | Async callbacks to OpenClaw |

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
         → AccountManager.run()   [sequential or multiprocessing]
         → FacebookPoster × N accounts
             ├─ login()                  [cookie restore → email/pass fallback]
             └─ publish_to_all_groups()
         → job_store.mark_done()
         → webhook.fire() [fire-and-forget]

Scheduled jobs: POST /schedule → DB → scheduler_runner (every 30s) → same pipeline
```

### Anti-Detection Stack

- **Patchright**: patched Chromium binary — hides `navigator.webdriver` at binary level
- **Emunium**: OS-level Bézier mouse curves. Used for clicks; typing uses `page.keyboard` (no OS focus dependency)
- **Fingerprints** (`fingerprints.json`): 15 profiles (Chrome 130–132, 6 LATAM+ES locales). One unique fingerprint per account, persisted in DB
- **Per-account identity**: UA, viewport, locale, timezone_id, color_scheme, `sec-ch-ua` headers, `hardwareConcurrency`, `deviceMemory`, `platform` via `add_init_script`
- **Typing**: log-normal delays, 1.5% typo rate with grouped correction (1–3 chars), 2% micro-pauses
- **Active hours**: per-account `(start, end)` tuple + `ZoneInfo` timezone — evaluated at execution time

### Database Schema (SQLite WAL, `threading.Lock` protected)

| Table | Purpose |
|-------|---------|
| `accounts` | PK: name. Fields: email (login ID: email or phone), groups (JSON), timezone, active_hours, fingerprint_json, password_enc, is_active |
| `account_cookies` | PK: email. Serialized session cookies (plaintext JSON — see known issues) |
| `jobs` | Queue: immediate + scheduled. Status: pending/running/done/failed/cancelled |
| `job_results` | Per-account, per-group success/failure with group_tag snapshot |
| `login_events` | Login audit trail |
| `gemini_usage` | Daily Gemini API quota tracking per account |
| `group_tags` | Human-readable emoji-safe labels per group ID |

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

### OpenClaw (X-API-Key required)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/accounts` | List active accounts + groups |
| POST | `/post` | Immediate publication (JSON or multipart) |
| POST | `/schedule` | Schedule publication (ISO 8601 `scheduled_for`) |
| GET | `/schedule` | List pending scheduled jobs |
| DELETE | `/schedule/<id>` | Cancel scheduled job |

### Admin (session cookie required)
| Method | Path | Description |
|--------|------|-------------|
| GET/POST | `/admin/login` | Login with ADMIN_KEY |
| GET | `/admin` | Admin SPA |
| GET | `/admin/publish` | Publishing interface |
| CRUD | `/admin/api/accounts[/<name>]` | Account management |
| PUT | `/admin/api/groups/<id>/tag` | Set group tag |
| GET | `/admin/api/history` | Login events |
| GET | `/admin/api/jobs` | Recent jobs |

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

## FacebookPoster Lifecycle

```python
poster = FacebookPoster(account, config)   # opens Chromium, loads fingerprint
if poster.login():                         # cookie restore → email/pass fallback
    results = poster.publish_to_all_groups(text, image_path=image_path)
poster.close()
```

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

## Known Issues

| Severity | Issue | Plan item |
|----------|-------|-----------|
| 🔴 Critical | All accounts share same IP — cluster-ban risk | 1.1 (proxy pool, needs hardware) |
| 🔴 Critical | Session cookies stored unencrypted in SQLite | 2.8 (Fernet encryption) |
| 🟡 Medium | Single `FB_PASSWORD` for all accounts | 1.2 (individual encrypted passwords) |
| 🟡 Medium | Flask dev server in production (no WSGI) | 2.4 (`waitress`) |
| 🟡 Medium | Dependencies unpinned (`>=` only, no lock file) | 2.5 (`pip freeze`) |
| 🟢 Low | Rate limiter in-memory (resets on restart) | 2.6 (SQLite-backed) |
| 🟢 Low | `sync_playwright` + threading not officially thread-safe | 3.1 (async migration) |

## Improvement Plan

Three phases tracked in `plan/` directory:

| Phase | Status | Focus |
|-------|--------|-------|
| **Fase 1** (stop-the-bleeding) | 3/6 complete | Identity isolation per account |
| **Fase 2** (hardening) | 0/9 | Production stability |
| **Fase 3** (refactor) | 0/7 | Async + FastAPI + observability |

**Completed:** 1.3 (fingerprints), 1.4 (timezone/active hours), 1.5 (typo rate)
**Pending critical:** 1.1 (proxies), 1.2 (crypto passwords), 2.4 (waitress), 2.5 (pin deps)

See `plan/AVANCE_FASE_*.md` for detailed task tracking.

## For Next Session

```bash
git log --oneline -10
sqlite3 facebook_auto_poster/jobs.db "SELECT id, status FROM jobs WHERE status='pending';"
tail -50 facebook_auto_poster/logs/main.log
```
