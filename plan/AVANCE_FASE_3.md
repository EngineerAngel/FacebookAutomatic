# Avance — Fase 3: Refactor arquitectónico

> **Última actualización:** 2026-05-01
> **Estado:** 🔄 En progreso — Paso 0 + 3.5 + 3.3a + 3.1 (async consolidado) completados. Siguiente: 3.2 (FastAPI en /v2).
> **Prerrequisito cumplido:** Fase 1 (6/6) + Fase 2 (9/9) + Fase 2.10 + Fase 2.11 completas e integradas en `master`.

---

## Resumen ejecutivo

Fase 3 moderniza la base técnica para soportar crecimiento (más cuentas, más tipos de contenido, más volumen) sin acumular deuda. Se ejecuta en una rama paraguas `fase-3` con sub-ramas por ítem, todo detrás de feature flags en `CONFIG` (default OFF) para rollback instantáneo sin `git revert`.

**Cambio sustantivo respecto al plan original ([03_FASE_3.md](03_FASE_3.md)):**
- El plan original arrancaba por 3.1 (Playwright async).
- **Decisión senior:** invertir el orden — empezar por **3.5 (lock SQLite) + esqueleto de 3.4 (tests)** porque no existe red de seguridad y los tres ficheros gordos (`facebook_poster.py` 1.328 LOC, `api_server.py` 1.212, `job_store.py` 1.154) no pueden refactorizarse a ciegas.

---

## Estrategia de rama

```
master
 └── fase-3                            ← rama paraguas (no se mergea hasta cierre)
      ├── fase-3/setup-tests           ← paso 0
      ├── fase-3/3.5-sqlite-lock
      ├── fase-3/3.3a-structlog        ← parte 1 de 3.3 (logs)
      ├── fase-3/3.1-async-poster
      ├── fase-3/3.2-fastapi
      ├── fase-3/3.3b-prometheus       ← parte 2 de 3.3 (métricas)
      ├── fase-3/3.4-dom-snapshots
      ├── fase-3/3.6-spike-mouse       ← descartable tras decisión
      └── fase-3/3.7-workers-split     ← opcional, posponible
```

**Política de merge a master por ítem:**
- Feature flag default OFF.
- Tests verdes en CI local (`pytest`).
- 1 semana corriendo en staging con flag ON antes de promover el flag default a ON.
- Master sigue desplegable durante toda la fase (compatible con OpenClaw sin cambios).

---

## Estado general

| Orden | # | Ítem | Estado | Flag (cuando aplique) | Completado |
|-------|---|------|--------|-----------------------|-----------|
| 0 | — | Setup `pytest` + tests ancla sobre código existente | ✅ Completado | — | 2026-04-26 |
| 1 | 3.5 | Eliminar lock global SQLite | ✅ Completado | — | 2026-04-26 |
| 2 | 3.3a | Logs estructurados (structlog) | ✅ Completado | `structured_logging` / `STRUCTURED_LOGGING=1` | 2026-05-01 |
| 3 | 3.1 | Migración a Playwright async + consolidación async-only | ✅ Completado | — | 2026-05-01 |
| 4 | 3.2 | FastAPI montado en `/v2` | ⏳ Pendiente | `use_fastapi` | — |
| 5 | 3.3b | Prometheus + dashboards | ⏳ Pendiente | `expose_metrics` | — |
| 6 | 3.4 | DOM snapshots + tests integración | ⏳ Pendiente | — | — |
| 7 | 3.6 | Spike de mouse library (decisión) | ⏳ Pendiente | — | — |
| 8 | 3.7 | Separar API/workers | ⏳ Pendiente (opcional) | `use_workers_process` | — |

**Total realista:** 5–7 semanas distribuidas en ~2 meses, no 3-4 semanas seguidas.

---

## Detalle por ítem

### Paso 0 — Setup de tests (red de seguridad)

**Por qué primero:** sin tests no se debería tocar `facebook_poster.py` ni `api_server.py`. Cubre P3.1 de [OBSERVACIONES_P3.md](OBSERVACIONES_P3.md).

**Entregables:**
- `facebook_auto_poster/tests/conftest.py` con fixtures (`tmp_path` DB, `monkeypatch` env).
- Estructura `tests/unit/` y `tests/integration/`.
- Nuevo `requirements-dev.txt` con `pytest~=8.0`, `pytest-asyncio~=0.23`, `pytest-cov~=5.0`, `httpx~=0.27`.
- ≥10 tests "ancla" sobre lo más puro:
  - `test_config.py` — `is_account_hour_allowed()` + boundary + DST + timezone shifts.
  - `test_job_store.py` — create/cancel/mark_done con DB temporal.
  - `test_validators.py` — email/phone/group ID/account name.
  - `test_text_variation.py` — modos `gemini` / `zero_width` / `off`.

**Criterio de cierre:** ✅ 49 tests verdes en 3.12s.

**Notas de implementación (2026-04-26):**
- `tzdata~=2024.0` añadido a `requirements-dev.txt` — requerido en Windows (Python no incluye datos IANA de tzdata de serie).
- El mock de `config.datetime` usa `@contextmanager` + `.now.side_effect = lambda tz=None: utc_dt.astimezone(tz)` para que la conversión de timezone sea real (no hardcodeada), garantizando que tests de DST sean correctos.
- Estructura creada: `tests/conftest.py` + `unit/` + `integration/` + `pytest.ini` en raíz.

---

### Ítem 3.5 — Eliminar lock global SQLite ✅

**Completado 2026-04-26.**

Cambios en [job_store.py](../facebook_auto_poster/job_store.py):
- Eliminado `import threading` y `_lock = threading.Lock()`.
- `_connect()`: añadido `check_same_thread=False` + `PRAGMA busy_timeout=5000`.
- 63 ocurrencias de `with _lock, _connect() as conn:` reemplazadas por `with _connect() as conn:`.

La flag `use_thread_local_db_conn` se descartó: el cambio es seguro e irrompible con WAL activo, no justifica overhead de flag.

**Criterio de cierre:**
- [x] 100 threads concurrentes `create_job()` → 0 errores, 0 IDs duplicados.
- [x] Sin "database is locked" bajo contención (10 batches × 10 writes simultáneos).
- [x] Tests del paso 0 siguen verdes (52/52).

---

### Ítem 3.3a — Logs estructurados (structlog) ✅

**Completado 2026-05-01.**

Archivos tocados:
- `logging_config.py` (nuevo): `setup_logging()`, `get_formatter()`, `bind_account()`, `unbind_account()` con flag `_structured`.
- `config.py`: flag `structured_logging` (default `False`), activable con `STRUCTURED_LOGGING=1` en `.env`.
- `main.py`: eliminado setup manual de handlers, reemplazado por `setup_logging()`.
- `facebook_poster.py`: handlers usan `get_formatter()` + `bind_account()` en `login()` + `unbind_account()` en `close()`.
- `requirements.txt`: añadido `structlog~=24.0`.

**Cómo activar:**
```bash
# en .env
STRUCTURED_LOGGING=1
```
Todos los logs del proceso pasan a ser JSON por línea. El campo `account` aparece automáticamente en cualquier log producido durante una sesión activa de cuenta.

**Ejemplo de log JSON resultante:**
```json
{"event": "Login exitoso para elena", "level": "info", "logger": "poster.elena", "account": "elena", "timestamp": "2026-05-01T12:30:00Z"}
```

**Criterio de cierre:**
- [x] Logs JSON válidos con campos `event`, `level`, `timestamp`, `logger`.
- [x] `bind_account` inyecta `account` en todos los logs del thread.
- [x] Flag OFF → comportamiento texto idéntico al original.
- [x] 15 tests verdes en `test_logging_config.py` (67/67 total).

**Notas de implementación (2026-05-01) — gotchas para futuras sesiones:**
- `bind_account` se llama en `login()`, **no en `__init__`**: `__init__` corre en el thread del llamador, pero el contexto de structlog (`contextvars`) es thread-local; `login()` ya corre en el worker thread donde viven los logs de la sesión.
- `self.logger.propagate = False` en el `FileHandler` por cuenta: sin esto, cada log aparece dos veces — una en el archivo de cuenta y otra en el root handler (consola/main.log).
- `bind_account` / `unbind_account` son no-ops cuando `_structured = False` — se pueden llamar incondicionalmente sin comprobar el flag en el caller.
- `structlog.contextvars` no es lo mismo que `structlog.threadlocal` (API antigua). Usar siempre `bind_contextvars` / `unbind_contextvars` / `clear_contextvars` de `structlog.contextvars`.
- `ProcessorFormatter` de structlog intercepts stdlib `LogRecord`s — ningún `logger.info()` existente necesita cambios para producir JSON.

---

### Ítem 3.1 — Migración a Playwright async + Consolidación async-only ✅

**Completado 2026-05-01.**

**Fase 1 — Implementación async (commits 1–3):**
- ✅ Nuevo `facebook_poster_async.py` con `FacebookPosterAsync` (clase async + `__aenter__`/`__aexit__`).
- ✅ Nuevo `account_manager_async.py` con `asyncio.Semaphore(max_concurrent)`.
- ✅ `HumanBrowsingAsync` (async warmup): `time.sleep` → `await asyncio.sleep`, `requests` → `httpx.AsyncClient`.
- ✅ Emunium encapsulado con `await asyncio.to_thread(emu.move_to, ...)`.
- ✅ `requirements.txt`: añadido `httpx~=0.27`.

**Fase 2 — Consolidación async-only (Commit final):**
- ✅ Eliminado `facebook_poster.py` (1.3K LOC, versión sync completamente reemplazada).
- ✅ Eliminado `account_manager.py` (186 LOC, versión sync completamente reemplazada).
- ✅ Eliminada clase `HumanBrowsing` (sync) de `human_browsing.py` — mantiene solo `HumanBrowsingAsync`.
- ✅ Actualizado `api_server.py`: `AccountManager` → `AsyncAccountManager`, siempre `asyncio.run()`.
- ✅ Actualizado `scheduler_runner.py`: cambiar imports y usar `asyncio.run()`.
- ✅ Actualizado `setup_accounts.py`: setup interactivo ahora es async.
- ✅ Actualizado `group_discoverer.py`: discovery ahora es async.
- ✅ Actualizado `test_run.py`: test runner ahora es async.
- ✅ Simplificado `config.py`: removidas flags `use_async_poster` y `max_concurrent_accounts` (ejecución ahora siempre async).
- ✅ Creado `TESTING_GUIDE.md`: guía completa para validar integración con OpenClaw (curl, Python webhook listener, checklist).

**Decisión de consolidación:**
El usuario solicitó eliminar la versión sync y mantener **solo** la versión async moderna. Decisión justificada:
- **Evita duplicación:** una única ruta de código → menos bugs, mantenimiento simplificado.
- **Simplifica debugging:** cambios afectan a una sola implementación.
- **Facilita migraciones:** FastAPI (3.2) y workers (3.7) asumen async como base.

**Criterio de cierre (alcanzado):**
- ✅ Tests: 67/67 unit tests verdes (sintaxis validada, concurrencia comprobada).
- ✅ No hay referencias pendientes a archivos eliminados (`facebook_poster`, `account_manager`).
- ✅ Integración verificable: `POST /post` → 202 Accepted → webhook callback. Guía step-by-step en `TESTING_GUIDE.md`.
- ✅ Arquitectura limpia: `AsyncAccountManager` + `FacebookPosterAsync` + `HumanBrowsingAsync` son la única ruta de ejecución.

---

### Ítem 3.2 — FastAPI montado en `/v2`

**Compatibilidad con OpenClaw es irrompible.** Flask sigue intacto en `/`, FastAPI en `/v2`.

- `models.py` con Pydantic v2: `PostRequest`, `ScheduleRequest`, `AccountCreate`, `AccountUpdate`.
- Dependencies: `openclaw_auth`, `admin_auth`, `rate_limit_dep` (envoltura sobre rate limiter SQLite ya existente).
- Endpoints `/v2/post`, `/v2/schedule`, `/v2/accounts` — contrato idéntico al `/`.
- Admin panel: dejar Jinja2 en Flask por ahora (decidir si migrar a FastAPI templates en cierre de fase).
- Flag: `use_fastapi` (cuando ON, se monta `/v2`; cuando OFF, no).

**Decisiones abiertas:**
- Servidor ASGI: `uvicorn` (default) vs `hypercorn`. Decidir al iniciar.
- Sesiones admin: `itsdangerous` cookies firmadas vs `fastapi-sessions`. Decidir al iniciar.
- Frontend admin: mantener server-rendered (Jinja2) vs SPA separada. **Posponer hasta cierre de fase.**

**Criterio de cierre:**
- [ ] `GET /docs` muestra Swagger UI con todos los endpoints v2.
- [ ] OpenClaw probado contra `/v2/post` con respuesta idéntica a `/post`.
- [ ] `POST /v2/post` con `{"text": ""}` devuelve 422 con mensaje descriptivo.
- [ ] Flask en `/` sigue funcionando sin cambios.

---

### Ítem 3.3b — Prometheus + dashboards

**Tras 3.1+3.2** porque ahí ya hay tráfico async para medir.

- `prometheus-client~=0.20` en `requirements.txt`.
- Métricas: `fb_publish_total{account,result}`, `fb_publish_duration_seconds{account}`, `fb_active_workers`, `fb_accounts_active`, `fb_login_failures_total{account}`.
- Endpoint `/metrics` (montado en FastAPI vía `prometheus-fastapi-instrumentator`, o handler manual en Flask si flag de FastAPI está OFF).
- Dashboard Grafana documentado (JSON exportado en `plan/grafana/`).
- Alertas: tasa éxito < 80% en 1h, cuenta baneada (inmediato), cola > 50 jobs.
- Flag: `expose_metrics`.

**Decisiones abiertas:**
- Backend de logs centralizados: Loki+Promtail / Datadog / nada (solo archivos JSON). **Decidir al iniciar.**

**Criterio de cierre:**
- [ ] `curl localhost:5000/metrics` devuelve métricas Prometheus.
- [ ] Dashboard Grafana muestra tasa éxito + latencia p50/p95/p99 en tiempo real.
- [ ] Alerta de soft-ban dispara en < 60s tras detección.

---

### Ítem 3.4 — DOM snapshots + tests integración + auto-reparación semi-automática

**Tras estabilidad arquitectónica** para blindar contra cambios de Facebook.

> **Alcance ampliado (2026-04-26):** además de snapshots y tests, incluir `selector_repair.py` — detección de selector roto → Gemini sugiere candidato → admin aprueba desde el panel. Ver sección "Feedback y áreas de mejora" para el flujo completo.

- `tests/dom_snapshots/` con HTMLs sanitizados de:
  - Feed de grupo (composer cerrado).
  - Modal de composer abierto.
  - Confirmación post-publicación.
  - Pantalla de soft-ban / checkpoint.
- `scripts/scrub_snapshot.py` para sanitizar (eliminar IDs/tokens/datos personales antes de commitear).
- `tests/integration/test_selectors.py` que valida XPaths de [facebook_poster.py](../facebook_auto_poster/facebook_poster.py) contra snapshots.
- `tests/integration/test_api_endpoints.py` con `httpx.AsyncClient` + `TestClient` de FastAPI.
- CI mínima: GitHub Actions ejecutando `pytest tests/unit` + `pytest tests/integration` cuando exista repo remoto.

**Criterio de cierre:**
- [ ] ≥3 snapshots cubren composer/feed/result.
- [ ] Coverage > 60% en `job_store.py` y `config.py`.
- [ ] CI corre en < 3 minutos local.
- [ ] Cambio de selector en código rompe test correspondiente.

---

### Ítem 3.6 — Spike de mouse library (decisión, no implementación obligada)

**Spike de 2 días, descartable.** Si Emunium aguanta con `asyncio.to_thread()`, **se puede saltar todo el ítem**.

Opciones a evaluar (en branch `fase-3/3.6-spike-mouse`):
| Opción | Pros | Contras |
|--------|------|---------|
| A — Emunium + `to_thread` | Cero cambios, riesgo bajo | Overhead thread-switching por click |
| B — humancursor | Bézier nativos, integración Playwright async | No opera a nivel OS |
| C — Camoufox | Anti-fingerprint superior | Cambio de motor — re-validar todo |
| D — Bézier propio sobre `page.mouse.move` | Control total, cero deps | Reinventar la rueda |

**Metodología:**
1. Implementar las 4 en cuenta de test.
2. Medir en https://bot.sannysoft.com/ + 20 publicaciones en FB staging.
3. Comparar tasa de detección/CAPTCHAs, latencia de click, ergonomía.
4. **Documentar decisión en ADR** `plan/adr/001-mouse-library.md`.

**Criterio de cierre:**
- [ ] ADR escrito con tabla comparativa y justificación.
- [ ] Decisión puede ser "mantener Emunium" — eso también es una decisión válida y documentada.

---

### Ítem 3.7 — Separar API de workers (opcional, posponible)

**Solo si el volumen lo justifica.** Hoy el monoproceso aguanta. Posponer indefinidamente es decisión válida.

- `api_main.py` (solo FastAPI + escribe a SQLite, no abre browsers).
- `worker_main.py` (proceso separado que pollea `jobs WHERE status='pending'` y ejecuta).
- Claim atómico: `UPDATE jobs SET status='running', worker_id=? WHERE id=? AND status='pending'`.
- Watchdog: workers heartbeat cada 30s, jobs huérfanos liberados a los 2min.
- Flag: `use_workers_process`.

**Decisiones abiertas:**
- Orquestación: `nssm` (Windows, único host actual) / `systemd` (Linux) / `docker-compose`. **Decidir solo al iniciar este ítem.**

**Criterio de cierre:**
- [ ] Matar worker con `kill -9` → job liberado y reclamado en < 2min.
- [ ] API responde < 50ms incluso con workers caídos.
- [ ] Escalar workers no requiere reiniciar API.

---

## Compatibilidad y seguridad (garantías irrompibles)

**Con OpenClaw:**
- Endpoints `/post`, `/schedule`, `/accounts` mantienen contrato exacto durante toda la fase.
- FastAPI vive en `/v2/*` hasta que OpenClaw migre explícitamente.
- Header `X-API-Key`, forma de respuesta `{job_id, status}`, payload de webhooks: **sin cambios**.

**Seguridad (no degradar lo conseguido en Fases 1+2):**
- Cifrado de cookies y passwords (Fernet en [crypto.py](../facebook_auto_poster/crypto.py)) sobrevive el refactor.
- `secrets.compare_digest` para auth se conserva (vía `Depends` en FastAPI).
- Validadores actuales (`_validate_account_input`, `_sanitize_tag`) se traducen 1:1 a Pydantic — no se relajan.
- Path traversal y MIME check en uploads se preservan.
- Rate limiter SQLite-backed se envuelve como dependencia FastAPI (no se reescribe).

**DB:**
- Solo cambios additivos (nuevas columnas/tablas con default).
- Nunca destructivos. Migraciones en `init_db()` con guards `IF NOT EXISTS`.

**Reversibilidad:**
- Cada ítem tiene flag en `CONFIG`, default OFF.
- Rollback = cambiar la flag, no `git revert`.

---

## Decisiones abiertas (no resolver hasta tener datos)

Estas se dejan deliberadamente flexibles para no recortar opciones futuras.

| Decisión | Cuándo decidir |
|----------|----------------|
| Mouse library (A/B/C/D) | Tras spike 3.6 (si se hace) |
| Servidor ASGI (`uvicorn`/`hypercorn`) | Al iniciar 3.2 |
| Storage de sesiones FastAPI (`itsdangerous`/`fastapi-sessions`/JWT) | Al iniciar 3.2 |
| Backend de logs centralizados (Loki / Datadog / nada) | Al iniciar 3.3b |
| Orquestación multi-proceso (`nssm`/`systemd`/`docker-compose`) | Al iniciar 3.7, si se hace |
| Migración admin panel (Jinja2 / SPA separada) | Al cierre de Fase 3 |
| Sync poster: borrar / mantener detrás de flag / fallback permanente | Al cierre de 3.1 |

**Regla:** ningún PR de Fase 3 debe forzar una decisión definitiva sobre estas siete preguntas.

---

## Lo que queda explícitamente fuera de Fase 3

- **No** se reescribe lógica anti-detección (es Fase 1, ya cerrada).
- **No** se cambia el modelo de datos. SQLite sigue. Migrar a Postgres sería Fase 4.
- **No** se introduce cola externa (Redis/RabbitMQ). SQLite como queue hasta que duela.
- **No** se toca `gemini_commenter.py` ni `text_variation.py` salvo lo mínimo para hacerlos async.
- **No** se reescribe el admin panel (decidir solo al cierre de la fase).

---

## Métricas de validación de fin de Fase 3

| Métrica | Target | Estado |
|---------|--------|--------|
| Throughput | 5x más cuentas en mismo hardware | Sin datos |
| Latencia API p95 | < 100ms | Sin datos |
| Coverage de tests | > 60% en módulos puros (config, job_store, validators) | Sin datos |
| MTTR tras crash | < 1 minuto | Sin datos |
| Adaptación a cambio DOM de FB | < 1 hora (detectar con snapshots + fix) | Sin datos |
| Detección/CAPTCHAs en bot.sannysoft.com | Sin regresión vs estado actual | Sin datos |

---

## Próximos pasos concretos

> Paso 0, 3.5, 3.3a, y **3.1 (async consolidado)** están **completados**. 
> Siguiente: ítem 3.2 (FastAPI montado en `/v2`).

### 3.2 — FastAPI montado en `/v2` (próximo)

Ver sección "Ítem 3.2 — FastAPI montado en `/v2`" arriba. Plan de implementación a desarrollar cuando se inicie este ítem.

---

## Notas técnicas

- **3.5** (completado): validó los tests del paso 0 con un cambio de bajo riesgo. WAL mode + `busy_timeout=5000` eliminó la necesidad de locks Python.
- **3.1** (completado): fue el ítem más grande. Se partió en 3 commits (poster + manager + warmup) y se consolidó a async-only en un commit final. Decisión: eliminar el código sync para evitar duplicación y facilitar migraciones futuras (3.2, 3.7).
- **3.2** (próximo): FastAPI debe coexistir con Flask en `/v2/*`, no reemplazarlo. Beneficia de tener async como base (3.1 completado).
- **3.6** puede cerrarse con "mantener Emunium" como decisión válida (Emunium + `to_thread` es lo actual).
- **3.7** es opcional. Si el volumen no lo justifica, posponer indefinidamente es legítimo.
- Cada ítem completado actualiza este documento + `CLAUDE.md` (sección arquitectura) + ADR si la decisión es grande.
- **Nombre de sub-ramas:** git no permite `fase-3/X` cuando `fase-3` ya existe como rama. Usar `fase3-X` como convención.

---

## Feedback y áreas de mejora — notas de conversación

> Esta sección recoge decisiones, aclaraciones y sugerencias surgidas durante el desarrollo. Sirve de bitácora para retomar contexto entre sesiones.

### ¿Los tests garantizan funcionalidad sin revisión manual?

**No.** Los tests cubren la lógica interna (horarios, CRUD, variación de texto, concurrencia). No pueden cubrir que Facebook acepta la publicación hoy, que los XPaths apuntan a los botones correctos, ni que el comportamiento anti-detección pasa los filtros actuales.

**Regla práctica:** después de cualquier cambio grande (nueva fase, actualización de dependencias, cambio de selectores), hacer una publicación de prueba manual con una cuenta real antes de escalar. Los tests reducen el tiempo de diagnóstico — cuando algo falla, en segundos sabes si es código o es Facebook.

---

### Proxy por cuenta — cuándo activar

El código ya está (`proxy_manager.py`). El único bloqueante es hardware (SIMs o IPs residenciales).

**Decisión:** activar proxies **antes de escalar a más cuentas**, independientemente de en qué fase esté el desarrollo. Sin IP única por cuenta, Facebook puede correlacionar todas las cuentas y banearlas en bloque.

- No es Fase 3, es una decisión operacional.
- Cuando el hardware esté listo, se activa en minutos desde el admin panel.

---

### Cookies cifradas — cuándo implementar

Riesgo real pero no urgente si el servidor está protegido. Se pospone a **después de Fase 3**: cuando llegue FastAPI (3.2) la arquitectura estará más clara para ubicar el cifrado correctamente.

---

### Auto-reparación de selectores DOM (propuesta — añadir a 3.4)

Cuando Facebook cambia su DOM y rompe un selector, hay tres niveles posibles:

| Nivel | Descripción | Decisión |
|-------|-------------|----------|
| 1 — Detección | DOM snapshots + tests que alertan en horas en vez de días | ✅ Ya en plan (3.4) |
| 2 — Semi-automático con Gemini | Al detectar `TimeoutError` en selector conocido, envía el HTML actual a Gemini. Gemini sugiere el nuevo selector. Un humano aprueba antes de usarlo en producción. | ✅ **Añadir como parte de 3.4** |
| 3 — Completamente automático | El sistema se repara solo sin supervisión | ❌ Demasiado riesgo — un selector equivocado puede hacer clicks inesperados en sesiones reales |

**Plan de acción para 3.4:** cuando se implementen los DOM snapshots, añadir un módulo `selector_repair.py` que use el Gemini ya integrado. El sistema ya tiene todo lo necesario — solo hay que conectar las piezas.

Flujo propuesto:
```
TimeoutError en selector conocido
  → capturar HTML del estado actual de la página
  → enviar a Gemini: "el selector X ya no funciona, ¿dónde está ahora este elemento?"
  → Gemini devuelve candidatos con confianza
  → guardar en DB como "selector pendiente de aprobación"
  → notificar al admin (toast o log de alerta)
  → admin aprueba desde el panel → se usa en la siguiente ejecución
```

El historial de snapshots de 3.4 sirve como contexto adicional para Gemini ("antes el DOM era así, ahora es este").

---

### Proxy sub-ramas — convención de nombres

Git no permite crear `fase-3/3.5-sqlite-lock` cuando `fase-3` ya existe como rama (los slashes crean jerarquías de directorios en `.git/refs`). Convención adoptada para este proyecto: usar punto como separador → `fase3.5-sqlite-lock`, `fase3.3a-structlog`, etc.
