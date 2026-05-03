# Revisión de Fallos y Conflictos — Orquestador (adaptado a Fase 3 async)

> **Basado en:** `ORCHESTRATOR_DESIGN.md` (versión async)
> **Última revisión:** Mayo 2026
> **Reemplaza:** versión anterior basada en threading/sync

---

## 0. Notas previas — qué cambió respecto a la revisión original

La revisión anterior asumía `threading.Lock`, `queue.Queue` y `FacebookPoster` sync. Esta versión:

- Reemplaza todos los locks/colas a `asyncio.*`
- Marca como **🟢 Ya resuelto** lo que el sistema actual ya cubre (orphan recovery, account locks, atomic CAS, `is_logged_in`, `webhook.fire_account_banned`)
- Elimina rediseños redundantes con el sistema actual
- Conserva los rediseños críticos (F2 → tier deriva del score, F3 → espera activa, F4 → cola por cuenta SQL, F9 → lock asyncio Emunium)

---

## 1. Fallos de concurrencia (asyncio)

### F1 — Idle action sin `_job_lock` ⚠️ CRÍTICO

**Problema:** `_idle_loop` ejecuta acciones idle en sesiones IDLE. Si un `dispatch` llega exactamente mientras una acción idle está corriendo, dos corrutinas tocan el mismo `page` async.

**Solución (asyncio):**

```python
class BrowserSession:
    _job_lock: asyncio.Lock

    async def do_idle_action(self) -> None:
        if self._job_lock.locked():
            return  # hay un job activo, saltar idle
        async with self._job_lock:
            # ejecutar acción idle
            ...

    async def publish_job(self, job) -> dict:
        async with self._job_lock:
            # publicación tiene prioridad por orden de llegada
            ...
```

`asyncio.Lock` no tiene `acquire(blocking=False)` — se usa `Lock.locked()` para chequear sin bloquear, o `asyncio.wait_for(lock.acquire(), timeout=0.001)` con try/except.

---

### F2 — Eliminado por rediseño ✅ APROBADO

**Decisión final:** No hay rotación programada de tiers. El tier se deriva del `health_score`:

```
score 80–100 → HOT
score 50–79  → WARM
score 0–49   → COLD
score = 0    → RESTRICTED
```

Cambios de tier ocurren sólo cuando cambia el score. Esto elimina F2 por completo.

---

### F3 — Espera activa en lugar de evicción forzada ✅ APROBADO

**Decisión final:** El pool nunca fuerza una evicción de sesión activa.

```python
async def evict_idle_lru(self) -> BrowserSession | None:
    candidates = [
        s for s in self.sessions.values()
        if s.state == SessionState.IDLE
    ]
    if not candidates:
        return None  # caller debe esperar — no forzar
    # LRU sobre IDLE: COLD primero, luego WARM, luego HOT
    candidates.sort(key=lambda s: (
        {"COLD": 0, "WARM": 1, "HOT": 2}[s.tier],
        s.last_activity,
    ))
    return candidates[0]
```

Caller pattern:

```python
while not (slot := await pool.evict_idle_lru()):
    await asyncio.sleep(30)
```

---

### F4 — Cola por cuenta vía SQL ✅ APROBADO

**Decisión final:** No `asyncio.Queue` en memoria. La cola por cuenta es una vista lógica sobre la tabla `jobs`:

```python
def claim_pending_job_for_account(account_name: str) -> dict | None:
    """Reclama el siguiente job pendiente para esta cuenta — sólo si no tiene otro running."""
    with _connect() as conn:
        # Si ya hay job 'running' para esta cuenta, no reclamar otro
        running = conn.execute(
            "SELECT 1 FROM jobs WHERE status='running' "
            "AND accounts LIKE ? LIMIT 1",
            (f'%"{account_name}"%',),
        ).fetchone()
        if running:
            return None
        # CAS atómico (mismo patrón que claim_pending_job)
        ...
```

**Garantía:** dos navegadores de la misma cuenta nunca pueden estar en `PUBLISHING` simultáneamente — la SQL lo bloquea. Cubre el escenario de "dos jobs casi simultáneos para la misma cuenta" sin necesitar locks en memoria.

---

## 2. Fallos de navegador

### F5 — Crash de Chromium ⚠️ CRÍTICO

**Problema:** Chromium puede crashear (OOM, bug Patchright, página colgada). El estado `IDLE` o `PUBLISHING` queda inconsistente con un `poster` muerto.

**Solución (asyncio):** task daemon de health check.

```python
async def _health_check_loop(self) -> None:
    while not self._stop_event.is_set():
        async with self.pool._lock:
            sessions = list(self.pool.sessions.values())
        for session in sessions:
            if not await session.health_ping():
                logger.error("Browser crash detectado: %s", session.account.name)
                session.state = SessionState.CLOSING
                if session._current_job_id:
                    job_store.mark_failed(
                        session._current_job_id,
                        "browser_crash",
                    )
                await self.pool.remove(session.account.name)
        await asyncio.sleep(30)


# en BrowserSession
async def health_ping(self) -> bool:
    if self.state == SessionState.PUBLISHING:
        return True  # no interrumpir publicación con ping
    try:
        await asyncio.wait_for(self.poster.page.title(), timeout=5)
        return True
    except Exception:
        return False
```

---

### F6 — Sesión expirada por Facebook ⚠️ MEDIO

**Problema:** Facebook puede expirar la sesión. El estado sigue `IDLE` pero al publicar redirige a login.

**Solución:** ya existe `_is_logged_in()` async en [facebook_poster_async.py:629](facebook_auto_poster/facebook_poster_async.py#L629). Sólo falta llamarla antes de cada publish:

```python
async def publish_job(self, job) -> dict:
    async with self._job_lock:
        if not await self.poster._is_logged_in():
            logger.warning("Sesión expirada para %s, re-login", self.account.name)
            self.state = SessionState.LOGGING_IN
            if not await self.poster.login():
                self.state = SessionState.RESTRICTED
                await health.record_event(self.account.name, "login_failed", -10)
                return {"status": "failed", "reason": "login_failed"}
        self.state = SessionState.PUBLISHING
        ...
```

---

### F7 — Login falla al crear sesión ⚠️ MEDIO

**Problema:** `create_session()` llama `login()`. Si falla (checkpoint, captcha, cookies inválidas), la sesión queda `LOGGING_IN` indefinida.

**Solución:**

```python
async def login(self) -> bool:
    self.state = SessionState.LOGGING_IN
    try:
        success = await self.poster.login()
    except Exception:
        logger.exception("Login excepción para %s", self.account.name)
        success = False
    if not success:
        self.state = SessionState.CLOSING
        await self.poster.close(really_close=True)
        return False
    self.state = SessionState.IDLE
    await health.record_event(self.account.name, "login_ok", +1)
    return True
```

`SessionPool.create_session()` debe verificar el resultado y eliminar la sesión del pool si retorna `False`.

---

### F8 — Checkpoint durante consumo ⚠️ MEDIO

**Problema:** `consume_feed_async()` y `consume_feed_cold_async()` (Fase B) podrían encontrar checkpoint mientras navegan. El sistema actual sólo detecta checkpoints durante `publish()`.

**Solución:** verificar URL después de cada acción de consumo.

```python
async def _check_for_checkpoint_async(self) -> None:
    url = self.page.url
    if any(token in url for token in ("/checkpoint/", "/login/", "/recover/")):
        raise FacebookCheckpointError(f"Checkpoint detectado: {url}")
```

Llamado al final de cada iteración del loop interno de `consume_feed_async`. La excepción se captura en el orquestador → marca sesión `RESTRICTED` y registra evento de health (-25).

---

## 3. Conflictos de Emunium

### F9 — Lock asyncio global de Emunium ✅ APROBADO

**Decisión final:** las sesiones se clasifican en dos categorías:

```
Con manos (usa Emunium): HOT publicando, "Like" en COLD
Pasivas (Playwright puro): scroll/hover/peek_comments en cualquier tier
```

Implementación:

```python
class SessionPool:
    _emunium_lock: asyncio.Lock   # global

    async def acquire_emunium(self) -> AbstractAsyncContextManager:
        return self._emunium_lock

# en BrowserSession (acción que usa Emunium)
async with self.pool._emunium_lock:
    # publish o react_to_random_post
    ...
```

Esto serializa las acciones con cursor del SO entre todas las sesiones — un humano sólo tiene un cursor. Las acciones pasivas (scroll programático con `page.mouse.wheel` o `page.evaluate`) no tocan el lock — pueden correr N en paralelo.

---

## 4. Fallos de base de datos

### F10 — Job huérfano al reiniciar ✅ YA RESUELTO

**Estado actual:** `mark_running_as_interrupted()` ya existe en [job_store.py:698](facebook_auto_poster/job_store.py#L698) y se llama desde:
- `worker_main.main()` al arranque ([worker_main.py:124](facebook_auto_poster/worker_main.py#L124))
- Handler SIGTERM/SIGINT del worker

No se necesita acción adicional. El orquestador hereda esto cuando vive dentro del worker.

---

### F11 — UPDATE atómico para health score ✅ APROBADO

**Solución:** todas las actualizaciones de score en una sola SQL statement.

```python
def adjust_health_score(account: str, delta: int) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE accounts SET health_score = MAX(0, MIN(100, health_score + ?)) "
            "WHERE name = ? RETURNING health_score",
            (delta, account),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
```

`RETURNING` (SQLite 3.35+, soportado por la versión bundled con Python 3.10+) devuelve el nuevo valor sin SELECT extra.

---

## 5. Sobre Multi-context Playwright

**Decisión:** **NO incluir en V1.**

Razones:

1. Cada cuenta tiene proxy SOCKS5 propio asignado por `proxy_manager`. Multi-context fuerza compartir proxy del Chromium padre — rompe la separación de IP por cuenta.
2. Cada cuenta tiene fingerprint único (UA, viewport, locale, timezone, sec-ch-ua). Patchright aplica fingerprint a nivel de browser, no de context. Compartir browser comparte fingerprint.
3. La RAM no es el cuello de botella actual (4 Chromiums × 400MB = 1.6GB; máquinas modernas tienen ≥8GB libre).

**Cuándo re-evaluar:** sólo si se escala a >10 cuentas activas simultáneas y el host tiene <16GB de RAM disponible. Incluso entonces, requeriría refactor profundo de `proxy_manager` y `fingerprints.json`.

---

## 6. Tabla resumen

| ID | Descripción | Severidad | Estado | Solución |
|----|-------------|-----------|--------|----------|
| F1 | Idle action sin lock | 🔴 Crítico | Por implementar | `asyncio.Lock` por sesión + check `locked()` |
| F2 | Rotación de tier durante publicación | — | ✅ Eliminado por rediseño | Tier deriva del health score |
| F3 | Evicción de sesión publicando | 🔴 Crítico | Aprobado | Sólo eviccionar sesiones IDLE; sino esperar |
| F4 | Dos jobs misma cuenta en paralelo | 🟡 Medio | Aprobado | Cola por cuenta vía SQL en `claim_pending_job_for_account` |
| F5 | Crash de Chromium | 🔴 Crítico | Por implementar | Health-ping task cada 30s |
| F6 | Sesión expirada por FB | 🟡 Medio | Por implementar (ya existe `_is_logged_in`) | Llamar antes de cada publish |
| F7 | Login falla al crear sesión | 🟡 Medio | Por implementar | Estado CLOSING + remover del pool |
| F8 | Checkpoint durante consumo | 🟡 Medio | Por implementar (Fase B) | Verificar URL después de cada acción |
| F9 | Dos sesiones con Emunium simultáneo | 🔴 Crítico | Aprobado | `asyncio.Lock` global + clasificación con/sin manos |
| F10 | Job huérfano al reiniciar | — | ✅ Ya resuelto en sistema actual | `mark_running_as_interrupted` ya implementado |
| F11 | Race condition en health score | 🟢 Bajo | Por implementar | UPDATE single-statement con `RETURNING` |

---

## 7. Decisiones de diseño confirmadas

> Plan abierto y adaptable — todas estas decisiones pueden revisarse con base en evidencia de producción.

### Arranque del sistema

- **Limpio siempre** — `cleanup_orphan_chromiums()` mata cualquier Chromium del proyecto al iniciar
- Sesiones iniciales creadas **escalonadas** (1 cada 30-60s, configurable)
- `mark_running_as_interrupted()` (ya existe) limpia jobs huérfanos en SQLite

### Jobs cuando no hay sesión disponible

- Si la cuenta no tiene sesión y el pool puede crecer: crear sesión (login asíncrono)
- Si el pool está lleno: esperar 30s e intentar `evict_idle_lru` — nunca forzar
- El job permanece en `pending` en SQLite mientras espera

### Jobs programados con cuenta RESTRINGIDA

```
Job programado → cuenta = RESTRICTED
        ↓
¿El job admite reasignación (campo `account_pool` en lugar de `accounts`)?
  Sí → escoger otra cuenta del pool con score >= 50
  No → posponer scheduled_for + 72h, registrar evento

Si tras 72h sigue restringida:
  → Nuevo intento de reasignación o postergación
  → Ciclo se repite hasta resolverse

Excepción admin: petición directa a esa cuenta intenta inmediato
  Si falla → reinicia ciclo de 72h
```

**Durante cooldown la cuenta puede ejecutar:**
- Discovery de grupos (no escribe)
- Idle browsing (lee feed)
- Cualquier flujo no-publish

### Tier derivado del health score

```
score 80–100 → HOT
score 50–79  → WARM
score 0–49   → COLD
score = 0    → RESTRICTED (cooldown 72h)
```

### Sesiones con y sin manos

```
HOT publicando → Emunium → adquiere _emunium_lock
COLD reaccionando → Emunium → adquiere _emunium_lock
WARM/COLD scrolling → Playwright puro → sin lock
```

### Cola por cuenta

Vista SQL sobre `jobs` filtrada por nombre. Persistente, recuperable ante crash.

### Notificaciones de restricción

Llegan a **dos destinos:**
- Panel admin: badge en la cuenta + entrada en `/admin/api/accounts/<name>/health`
- Webhook: `webhook.fire_account_banned()` (ya existe en [webhook.py:103](facebook_auto_poster/webhook.py#L103))

### Historial de eventos de salud

Registrado en **dos lugares:**
- `account_health_events` en SQLite — fuente de verdad
- `logs/{account}.log` — debug

`account_bans` (tabla legacy) **no se migra**; nuevos bans van a `account_health_events`.

---

## 8. Orden de incorporación

**Antes de Fase D (en el diseño base del orquestador):**
- F1 — `asyncio.Lock` por sesión
- F3 — espera activa en evict
- F4 — cola por cuenta SQL
- F9 — `_emunium_lock` global

**Durante Fase D:**
- F5 — health-ping task
- F6 — re-verificar login antes de publish
- F7 — manejo de fallo de login en create_session

**Durante Fase B (poster):**
- F8 — checkpoint detection en consume_feed

**Durante Fase A (health):**
- F11 — UPDATE atómico con RETURNING

**Sin acción (ya resuelto):**
- F10 — orphan recovery
- F2 — rotación 4h (eliminada)

---

## 9. Reglas estrictas para la sesión de implementación

> Estas reglas se aplican a **todas las fases** del orquestador. Heredadas de [CLAUDE.md](../CLAUDE.md) "Session Rules" y "Protocolo para implementaciones grandes".

1. Solo implementar lo que se pide explícitamente — nada más.
2. Si encuentras algo "que podría mejorarse" fuera del alcance: reportarlo como nota al final, no implementarlo.
3. Antes de crear archivo nuevo: confirmar que no existe similar.
4. Antes de modificar archivo existente: leer su contenido completo.
5. No refactorizar código fuera del alcance.
6. No agregar imports/dependencias/funciones auxiliares no solicitadas.
7. Si una tarea requiere tocar más archivos de los esperados: pausar y reportar.
8. Al terminar cada paso: listar archivos tocados y no tocados, esperar confirmación.

**Archivos protegidos (read-only sin confirmación explícita):**

- `facebook_poster_async.py` — modificable sólo en Fases B y C, no en otras
- `human_browsing.py` — sólo lectura (Fase B reusa sus helpers internos)
- `gemini_commenter.py` — sólo lectura
- `webhook.py` — sólo lectura (ya tiene `fire_account_banned`)
- `proxy_manager.py` — sólo lectura
- `worker_main.py` — modificable sólo en Fase D (un solo branch condicional)

**Whitelist por fase:** ver tabla en `ORCHESTRATOR_DESIGN.md` §6. Cada fase tiene archivos explícitos. Tocar fuera de esa lista requiere parar y reportar.

---

## 10. Plan abierto

Todas estas decisiones pueden revisarse con evidencia de producción. Sin decisiones irreversibles. Si durante la implementación de cualquier fase se encuentra un conflicto no documentado: pausar, reportar, esperar confirmación antes de continuar.
