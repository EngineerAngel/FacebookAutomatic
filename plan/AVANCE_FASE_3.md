# Avance — Fase 3: Refactor arquitectónico

> **Última actualización:** 2026-05-01
> **Estado:** 🔄 En progreso — Paso 0 + 3.5 + 3.3a + 3.1 + 3.2 + 3.3b + 3.4 completados. Siguiente: 3.6 (spike mouse library, opcional) o cierre de Fase 3.
> **Prerrequisito cumplido:** Fase 1 (6/6) + Fase 2 (9/9) completas e integradas en `master`.

---

## Resumen ejecutivo

Fase 3 moderniza la base técnica para soportar crecimiento (más cuentas, más tipos de contenido, más volumen) sin acumular deuda. Se ejecuta en la rama `fase-3`.

**Orden de ejecución adoptado (vs plan original):**
- Plan original arrancaba por 3.1 (Playwright async).
- Decisión real: empezar por **3.5 (lock SQLite) + Paso 0 (tests)** para tener red de seguridad antes de tocar los módulos grandes.

---

## Estado general

| Orden | # | Ítem | Estado | Completado |
|-------|---|------|--------|-----------|
| 0 | — | Setup `pytest` + tests ancla | ✅ Completado | 2026-04-26 |
| 1 | 3.5 | Eliminar lock global SQLite | ✅ Completado | 2026-04-26 |
| 2 | 3.3a | Logs estructurados (structlog) | ✅ Completado | 2026-05-01 |
| 3 | 3.1 | Playwright async + consolidación async-only | ✅ Completado | 2026-05-01 |
| 4 | 3.2 | FastAPI montado en `/v2` + Pydantic validation | ✅ Completado | 2026-05-01 |
| 5 | 3.3b | Prometheus + Grafana + tab de métricas en admin | ✅ Completado | 2026-05-01 |
| 6 | 3.4 | DOM snapshots + auto-reparación selectores (con Scrapling) | ✅ Completado | 2026-05-01 |
| 7 | 3.6 | Spike mouse library | ⏳ Pendiente (puede saltarse) | — |
| 8 | 3.7 | Separar API/workers | ⏳ Pendiente (opcional) | — |

---

## Detalle por ítem

### Paso 0 — Setup de tests ✅

**Completado 2026-04-26.**

- `tests/conftest.py` con fixtures de DB temporal y env vars.
- Estructura `tests/unit/` y `tests/integration/`.
- `requirements-dev.txt`: `pytest~=8.0`, `pytest-asyncio~=0.23`, `pytest-cov~=5.0`, `httpx~=0.27`, `tzdata~=2024.0`.
- 67 unit tests actualmente (crecieron con 3.3a y 3.1).

**Gotcha:** `tzdata~=2024.0` es requerido en Windows — Python no incluye la base de datos IANA de timezones de serie.

---

### Ítem 3.5 — Eliminar lock global SQLite ✅

**Completado 2026-04-26.**

- Eliminado `threading.Lock()` global en `job_store.py`.
- `_connect()`: añadido `check_same_thread=False` + `PRAGMA busy_timeout=5000`.
- SQLite WAL maneja la concurrencia de forma nativa — el lock Python era redundante.

---

### Ítem 3.3a — Logs estructurados (structlog) ✅

**Completado 2026-05-01.**

- `logging_config.py`: `setup_logging()`, `bind_account()`, `unbind_account()`, `get_formatter()`.
- Activar con `STRUCTURED_LOGGING=1` en `.env`.
- Salida JSON por línea con campos `event`, `level`, `logger`, `account`, `timestamp`.

**Gotchas para futuras sesiones:**
- `bind_account` se llama en `login()` (no `__init__`) porque `contextvars` de structlog es thread-local.
- `self.logger.propagate = False` en FileHandler por cuenta — evita líneas duplicadas.
- Usar siempre `structlog.contextvars` (no la API `threadlocal` antigua).

---

### Ítem 3.1 — Playwright async + Consolidación async-only ✅

**Completado 2026-05-01.**

**Qué se hizo:**
- `facebook_poster_async.py`: `FacebookPosterAsync` (async + `__aenter__`/`__aexit__`, Emunium con `asyncio.to_thread`).
- `account_manager_async.py`: `AsyncAccountManager` con `asyncio.Semaphore`.
- `human_browsing.py`: solo queda `HumanBrowsingAsync` — clase sync eliminada.
- `facebook_poster.py` y `account_manager.py` **eliminados completamente**.
- Todos los callers migrados: `api_server.py`, `scheduler_runner.py`, `setup_accounts.py`, `group_discoverer.py`, `test_run.py`.
- `config.py`: removidas flags `use_async_poster` y `max_concurrent_accounts` — ejecución siempre async.
- `TESTING_GUIDE.md`: guía completa para verificar la integración con OpenClaw.

**Por qué se eliminó el código sync:**
Una única ruta de ejecución → menos bugs, menos mantenimiento, base más limpia para 3.2 y 3.7.

---

### Ítem 3.2 — FastAPI montado en `/v2` ✅

**Completado 2026-05-01.**

**Qué resuelve:** validación automática de datos de entrada, documentación interactiva en `/docs`, base más sólida para crecer.

**Qué NO cambia:** Flask sigue en `/`, OpenClaw no necesita cambiar nada.

**Decisiones técnicas implementadas:**
- Servidor ASGI: `uvicorn` (reemplaza `waitress` cuando `USE_FASTAPI=1`).
- Arquitectura: FastAPI como proceso principal + Flask montado con `WSGIMiddleware` — un solo proceso, un solo puerto.
- Panel admin: se queda en Flask/Jinja2.

**Archivos creados:**
- `v2_models.py` — Pydantic v2: `PostRequest`, `ScheduleRequest`, request/response models con field validators.
- `v2_deps.py` — dependencias: `verify_api_key()`, `check_rate_limit()` (reutiliza lógica de Flask).
- `v2_router.py` — endpoints `/v2/{accounts,post,schedule}` (idénticos a Flask pero con validación automática).
- `v2_app.py` — orquestación: FastAPI + Flask montado como sub-app.

**Validación completada:**
- ✅ `GET /v2/docs` muestra Swagger UI con todos los endpoints v2.
- ✅ `POST /v2/post` devuelve 202 Accepted (idéntico a `/post`).
- ✅ `POST /v2/post` con `{"text": ""}` devuelve 422 con detalle del error.
- ✅ Flask en `/` sigue funcionando sin cambios.
- ✅ Activable con `USE_FASTAPI=1` en `.env`.

---

### Ítem 3.3b — Prometheus + Grafana + Tab de métricas ✅

**Completado 2026-05-01.**

**Qué resuelve:** visibilidad en tiempo real de la ejecución — tasa de éxito, latencia, cuentas baneadas, cola de jobs.

**Implementación:**
- `metrics.py`: Counters (jobs, publishes, logins, API requests), Histograms (duración), Gauges (con Collector que consulta DB)
- Endpoint `GET /metrics` (formato Prometheus) — cero overhead si `METRICS_ENABLED=0`
- `docker-compose.monitoring.yml`: Prometheus + Grafana con dashboard pre-configurado
- Tab "Métricas" en admin panel: gauges en vivo + polling cada 3s desde `/admin/api/queue`
- Integración en `facebook_poster_async.py`, `api_server.py`: calls a `metrics.inc_login()`, `metrics.inc_publish()`, `metrics.observe_publish_duration()`

**Activación:** `METRICS_ENABLED=1` en `.env`

**Documentación:** ver `METRICS_SETUP.md` para setup completo y troubleshooting.

---

### Ítem 3.4 — DOM snapshots + auto-reparación de selectores ✅

**Completado 2026-05-01.**

**Archivos creados:**
- `adaptive_selector.py`: `AdaptivePlaywrightBridge` — genérico, sin deps de FB. 3 niveles: DB aprobado → Scrapling adaptive → Gemini fire-and-forget.
- `selector_repair.py`: `SelectorRepairService` — llama Gemini, parsea candidatos JSON, escribe a DB.
- `tests/unit/test_adaptive_selector.py`: 11 tests (bridge, helpers) — todos pasan.
- `tests/dom_snapshots/README.md`: instrucciones para capturar y sanitizar snapshots.
- `scripts/scrub_snapshot.py`: elimina IDs/tokens/scripts de HTMLs antes de commitear.

**Archivos modificados:**
- `job_store.py`: tabla `selector_repairs` + 5 funciones CRUD.
- `facebook_poster_async.py`: 6 selectores críticos envueltos con `AdaptivePlaywrightBridge`.
- `api_server.py`: endpoints `/admin/api/selector-repairs` (list/approve/reject).
- `templates/admin.html`: tab "Mantenimiento" con tabla de repairs pendientes.
- `config.py`: flag `adaptive_selectors_enabled`.
- `requirements.txt`: `scrapling>=0.3,<1.0` (sin `[fetchers]`).

**Flujo de auto-reparación:**
```
get_locator() → selector original falla (PatchrightTimeout)
  → Scrapling: busca por fingerprint adaptativo en HTML actual
     ├─ Encontrado → guarda repair(source='scrapling') en DB, devuelve locator
     └─ No encontrado → fire-and-forget SelectorRepairService (Gemini)
          → Gemini devuelve JSON de candidatos con confidence
          → guarda repairs(source='gemini', status='pending') en DB
          → admin aprueba en tab "Mantenimiento" → activo en próxima ejecución
```

**Activación:** `ADAPTIVE_SELECTORS=1` en `.env` (default OFF — sin la flag, zero overhead).

---

### Ítem 3.6 — Spike mouse library

**Puede saltarse** si Emunium + `asyncio.to_thread` funciona sin problemas en producción.

Opciones evaluables: Emunium+`to_thread` (actual) / humancursor / Camoufox / Bézier propio. Si se hace, documentar decisión en ADR.

---

### Ítem 3.7 — Separar API de workers (opcional)

**Solo si el volumen lo justifica.** Un monoproceso aguanta bien hasta decenas de cuentas.

- `api_main.py` solo recibe peticiones y escribe a SQLite.
- `worker_main.py` pollea jobs pendientes y ejecuta.
- Flag: `use_workers_process`.

---

## Garantías irrompibles

**OpenClaw:**
- Endpoints `/post`, `/schedule`, `/accounts` mantienen contrato exacto durante toda la fase.
- `X-API-Key`, payload de respuesta `{job_id, status}`, webhooks: sin cambios.

**Seguridad:**
- Cifrado Fernet (`crypto.py`) sobrevive el refactor.
- `secrets.compare_digest` se conserva en todos los paths de auth.
- Validadores de entrada se traducen 1:1 a Pydantic — no se relajan.

**DB:**
- Solo cambios aditivos (columnas/tablas nuevas con default, nunca destructivos).

---

## Decisiones abiertas

| Decisión | Cuándo decidir |
|----------|----------------|
| ¿Hacer 3.3b (Prometheus) o 3.4 (DOM repair) primero? | Al iniciar el próximo ítem |
| Mouse library (3.6) | Solo si Emunium falla en producción |
| Backend logs centralizados (Loki/Datadog/nada) | Al iniciar 3.3b |
| Orquestación multi-proceso (nssm/systemd/docker) | Al iniciar 3.7, si se hace |
| Migración admin panel (Jinja2 / SPA) | Al cierre de Fase 3 |

---

## Lo que queda fuera de Fase 3

- Lógica anti-detección (Fase 1, cerrada).
- Modelo de datos — SQLite sigue. Migrar a Postgres sería Fase 4.
- Cola externa (Redis/RabbitMQ) — SQLite como queue hasta que duela.
- Reescritura del admin panel.

---

## Métricas de validación al cierre

| Métrica | Target | Estado |
|---------|--------|--------|
| Throughput | 5x más cuentas en mismo hardware | Sin datos |
| Latencia API p95 | < 100ms | Sin datos |
| Coverage tests | > 60% en `job_store.py` y `config.py` | Sin datos |
| MTTR tras crash | < 1 minuto | Sin datos |
| Adaptación a cambio DOM | < 1 hora (detectar + fix) | Sin datos |

---

## Notas de conversación

### Tests vs revisión manual

Los tests cubren lógica interna (horarios, CRUD, concurrencia). **No cubren** que Facebook acepta la publicación hoy, que los XPaths funcionan, ni que el anti-detección pasa los filtros actuales.

Regla: después de cualquier cambio grande, hacer una publicación de prueba manual con cuenta real antes de escalar.

---

### Proxy por cuenta

Código ya existe (`proxy_manager.py`). Bloqueante: hardware (SIMs / IPs residenciales). Activar **antes de escalar a más de 3 cuentas** — sin IP por cuenta, Facebook puede correlacionar el cluster y banear todo de golpe.

---

### Cookies de sesión cifradas

Riesgo real pero no urgente si el servidor está protegido físicamente. Pospuesto a después de Fase 3.

---

### Convención de nombres de ramas

Git no permite `fase-3/3.X` cuando `fase-3` ya existe como rama. Convención adoptada: `fase3.X-nombre` (punto como separador).
