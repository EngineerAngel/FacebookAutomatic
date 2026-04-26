# Avance — Fase 3: Refactor arquitectónico

> **Última actualización:** 2026-04-26
> **Estado:** ⏳ No iniciada — plan de ejecución preparado, pendiente luz verde para crear rama
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
| 2 | 3.3a | Logs estructurados (structlog) | ⏳ Pendiente | `structured_logging` | — |
| 3 | 3.1 | Migración a Playwright async | ⏳ Pendiente | `use_async_poster` | — |
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

### Ítem 3.3a — Logs estructurados (structlog)

**Antes que el resto del refactor** porque facilita debuggear los pasos siguientes.

- Añadir `structlog~=24.0` a `requirements.txt`.
- `logging_config.py` con `JSONRenderer` + `merge_contextvars` + `dict_tracebacks`.
- Helper `bind_account(name)` para inyectar `account=name` en todos los logs del thread.
- Migrar logs críticos (login, publish, ban detection) a `logger.info("event_name", **fields)`.
- Flag: `structured_logging` (cuando OFF, sigue logging clásico).

**Criterio de cierre:**
- [ ] Logs de publish son 1 línea JSON por evento.
- [ ] Búsqueda `grep '"event":"publish_success"'` sobre `logs/main.log` funciona.
- [ ] Logs clásicos siguen siendo legibles en consola con flag OFF.

---

### Ítem 3.1 — Migración a Playwright async

**El más grande.** Se construye en paralelo al sync, no se reemplaza.

- Nuevo `facebook_poster_async.py` con `FacebookPosterAsync` (clase async + `__aenter__`/`__aexit__`).
- Nuevo `account_manager_async.py` con `asyncio.Semaphore(max_concurrent)`.
- Reescribir [human_browsing.py](../facebook_auto_poster/human_browsing.py) y [gemini_commenter.py](../facebook_auto_poster/gemini_commenter.py): `time.sleep` → `await asyncio.sleep`, `requests` → `httpx.AsyncClient`.
- Encapsular Emunium (sync) con `await asyncio.to_thread(emu.click, ...)` provisional. Decisión final en 3.6.
- Flag: `use_async_poster` (default OFF). Activar cuenta por cuenta vía DB.

**Coexistencia:**
- `facebook_poster.py` sync se mantiene **vivo todo el tiempo**. No se borra al cerrar 3.1 — se decide en cierre de Fase 3.
- Si flag OFF, comportamiento idéntico al actual.

**Criterio de cierre:**
- [ ] 1 cuenta corre 1 semana en async sin errores nuevos vs sync.
- [ ] Cancelación de un job en mitad de `publish()` no deja Chromes zombie.
- [ ] 5 cuentas paralelas: ~40% CPU / ~3GB RAM (vs 80%/5GB con multiprocessing actual).
- [ ] Flag default sigue OFF al merge.

**Decisiones abiertas (resolver durante implementación, no antes):**
- Cómo manejar Emunium (`to_thread` vs reemplazo en 3.6).
- Si la cancelación usa `asyncio.CancelledError` o un flag cooperativo (`self._stop_requested`).

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

### Ítem 3.4 — DOM snapshots + tests integración

**Tras estabilidad arquitectónica** para blindar contra cambios de Facebook.

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

## Próximos pasos concretos (cuando se dé luz verde)

1. `git checkout -b fase-3` desde `master`.
2. Crear `facebook_auto_poster/tests/conftest.py` + estructura `unit/` `integration/`.
3. Crear `requirements-dev.txt` con `pytest~=8.0`, `pytest-asyncio~=0.23`, `pytest-cov~=5.0`, `httpx~=0.27`.
4. Implementar los ≥10 tests ancla del Paso 0.
5. Abrir sub-branch `fase-3/3.5-sqlite-lock` como primer ejercicio de validación del flujo.

---

## Notas

- **3.5** es trivial pero educativo: valida los tests del paso 0 con un cambio de bajo riesgo.
- **3.1** es el ítem más grande — partir en 3 PRs internos a la rama paraguas (poster + manager + warmup/gemini).
- **3.6** puede cerrarse con "mantener Emunium" como decisión válida.
- **3.7** es opcional. Si el volumen no lo justifica, posponer indefinidamente es legítimo.
- Cada ítem completado actualiza este documento + `CLAUDE.md` (sección arquitectura) + ADR si la decisión es grande.
