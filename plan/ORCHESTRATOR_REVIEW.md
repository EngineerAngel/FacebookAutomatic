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
    # Sin excepción de estado — se hace ping siempre.
    # Si el Chromium está vivo puede responder aunque esté publicando o en login.
    # Si no responde en cualquier estado, es un crash real.
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
- Sesiones se crean según demanda del modelo de bloques — las cuentas con publicaciones más tempranas en el día arrancan primero. Sin escalonamiento fijo.
- `mark_running_as_interrupted()` (ya existe) limpia jobs huérfanos en SQLite

### Jobs cuando no hay sesión disponible

- Si la cuenta no tiene sesión y el pool puede crecer: crear sesión (login asíncrono)
- El job queda en estado `queued` (no `running`) mientras espera navegador o `active_hours`
- Si el pool está lleno y el tiempo estimado supera el umbral: notifica admin, job permanece en `queued`
- Evicción LRU solo sobre sesiones `IDLE` — nunca sobre `PUBLISHING` o `LOGGING_IN`

### Jobs programados con cuenta RESTRINGIDA

```
Job programado → cuenta = RESTRICTED
        ↓
Health score cancela todos los jobs pending y queued de esa cuenta
        ↓
Notificación al admin: "Cuenta X restringida — N jobs cancelados"
Webhook: fire_account_banned() (ya existe)
```

**Durante cooldown la cuenta puede ejecutar:**
- Discovery de grupos (no escribe)
- Idle browsing en navegador libre (consume contenido para recuperar score)
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

## 10. Fallos observados en el sistema actual (pre-orquestador)

> Estos fallos existen hoy, independientemente del orquestador. Se documentan aquí para tenerlos presentes al diseñar las soluciones en Fases C y D.

### FA-1 — Archivo `.lock` no se elimina si Chromium crashea

`close()` intenta cerrar `context` y `_pw` antes de eliminar el `.lock`. Si el Chromium ya murió, las dos primeras líneas lanzan excepción y `lock_file.unlink()` nunca se ejecuta.

```python
async def close(self):
    await self.context.close()   # ← falla si Chromium está muerto
    await self._pw.stop()        # ← también falla
    lock_file.unlink()           # ← nunca llega aquí
```

**Consecuencia:** Al reiniciar el worker, el perfil tiene `.lock` activo y puede rechazar abrir ese perfil de navegador.

**Solución en Fase C:** mover `lock_file.unlink()` a un bloque `finally` independiente dentro de `close()`.

---

### FA-2 — Sin timeout global en `asyncio.run(mgr.run())`

Si el Chromium se congela (no crashea, solo deja de responder), no lanza excepción. El thread queda esperando indefinidamente. El job permanece en estado `running` hasta que el proceso muere.

**Consecuencia:** jobs zombie que bloquean la cuenta indefinidamente.

**Solución en Fase D:** `asyncio.wait_for(mgr.run(), timeout=CONFIG["job_timeout_seconds"])`.

---

### FA-3 — El sistema tarda en detectar un navegador cerrado manualmente

**Observado en producción:** al cerrar el Chromium manualmente, el poster sigue intentando publicar. No se da cuenta de inmediato — continúa ejecutando operaciones en un navegador que ya no existe.

**Por qué tarda:** cada operación (`wait_for`, `locator`, `fill`) tiene su propio timeout (10-20s). El sistema espera ese timeout completo antes de lanzar excepción. Con varios reintentos en `AdaptivePlaywrightBridge`, puede acumular varios minutos antes de propagar el error.

**Al final sí falla y marca el job como `failed`** — pero el tiempo perdido es significativo y el log muestra actividad en un navegador muerto.

**Solución en Fase D:** el health-ping daemon (F5) detecta el navegador caído en el próximo ciclo de 30s y fuerza el cierre sin esperar los timeouts de cada operación.

---

### FA-4 — Sin detección proactiva de navegador caído

No existe ningún proceso que verifique si los Chromiums siguen vivos entre operaciones. La detección actual es **reactiva** — solo se sabe que el navegador murió cuando una operación específica falla.

El health-ping del orquestador (F5) convierte esto en **detección proactiva** cada 30s.

---

## 11. Plan abierto

Todas estas decisiones pueden revisarse con evidencia de producción. Sin decisiones irreversibles. Si durante la implementación de cualquier fase se encuentra un conflicto no documentado: pausar, reportar, esperar confirmación antes de continuar.

---

## 12. Decisiones de revisión — Mayo 2026

Resultado de la sesión de revisión crítica antes de implementar. Cada decisión reemplaza o complementa lo que estaba en el diseño original.

### 12.1 Estados de job con orquestador activo

El job **no se marca `running` hasta que hay navegador disponible Y la cuenta está en `active_hours`**. Se agrega el estado `queued` entre `pending` y `running`.

```
pending → queued → running → done / failed
```

Sin orquestador (`ORCHESTRATOR_ENABLED=0`): el flujo actual `pending → running` no cambia.

### 12.2 Modelo de navegadores y proxies

4 navegadores fijos, cada uno con proxy SOCKS5 dedicado y grupo de cuentas. Las cuentas pueden moverse entre navegadores si hay espacio. Evicción LRU sólo sobre sesiones `IDLE` — nunca sobre `PUBLISHING` o `LOGGING_IN`.

### 12.3 Estimación de tiempo

Estimación pesimista usando máximos del config:
```python
tiempo_por_grupo = wait_between_groups_max + consume_between_groups_max
tiempo_estimado  = n_grupos_pendientes * tiempo_por_grupo
```
Umbral para buscar otro navegador: **15 minutos**.

### 12.4 Jobs en espera — sistema lleno

- Job queda en `queued`, no se descarta
- Admin recibe notificación "Sistema lleno"
- Al desocuparse navegador: ejecuta sólo si la cuenta está en `active_hours`
- Si no está en horario activo: espera al siguiente día dentro de `active_hours`

### 12.5 Detección de duplicados

| Caso | Acción |
|------|--------|
| Misma cuenta + mismo grupo | DESCARTA job nuevo + notifica admin |
| Mismo grupo, diferente cuenta | Avisa admin, encola normalmente |
| Misma cuenta, diferente grupo | Encola sin notificación |

### 12.6 Health score + orquestador

El health score actúa como monitor independiente. Al detectar que una cuenta pasa a `RESTRICTED`:
1. Cancela todos sus jobs `pending` y `queued`
2. Si está publicando: termina el grupo actual y para (no continúa con grupos restantes)
3. Notifica al admin en el panel web

> **Webhook a OpenClaw (`fire_account_banned`):** pendiente — OpenClaw en mantenimiento. Prioridad actual es notificación en panel admin únicamente. Retomar cuando OpenClaw esté activo.

### 12.7 Modelo de bloques — reemplaza escalonamiento

El orquestador trabaja con bloques de tiempo, no con jobs individuales en tiempo real. Las publicaciones se planifican con antelación — al arrancar el sistema ya sabe qué va a pasar en cada bloque.

Jobs on-demand → se insertan en el próximo bloque disponible dentro de `active_hours`.

**Uso de navegadores en un bloque:**
- Cuentas HOT/WARM → publican
- Cuentas COLD → consumen contenido en navegadores libres (idle browsing para recuperar score)
- Cuentas RESTRICTED → sin sesión, en cooldown

**Priorización de cuentas COLD para navegadores libres:** score más cercano a 50 primero — las que están a punto de recuperarse tienen prioridad para usar el navegador y volver a WARM antes.

### 12.8 Emunium lock — acciones idle son Playwright puro

Las acciones de idle (scroll, hover, abrir comentarios) usan **Playwright puro** — sin Emunium, sin mouse físico. Solo dos acciones adquieren el `_emunium_lock`:
- HOT publicando (`publish()`)
- COLD/WARM reaccionando con Like (`_react_to_random_post_async`)

Esto permite que N sesiones hagan idle simultáneamente sin conflicto de cursor.

### 12.9 Layout de ventanas — automático al cambiar el pool

El orquestador recalcula y reposiciona todas las ventanas cada vez que el pool cambia (sesión creada o eliminada). La lógica vive en `window_layout.py` (archivo separado).

```
1 navegador  → pantalla completa
2 navegadores → 50% izquierda / 50% derecha
3 navegadores → 25% + 25% izquierda / 50% derecha
4 navegadores → cuadrícula 2×2 (25% cada uno)
```

Emunium usa estas posiciones fijas para calcular coordenadas absolutas de clic.

### 12.10 Filosofía de archivos

Un archivo por responsabilidad. `orchestrator_async.py` solo coordina — no implementa lógica propia de más de ~20 líneas. Lo que crece va a su propio módulo. Aplica a todo lo que se implemente.

### 12.11 Verificación de login — una vez al inicio

La verificación `_is_logged_in()` se llama **una sola vez** antes de `publish_to_all_groups()`, desde `BrowserSession.publish_job()` en el orquestador — no dentro del poster.

Verificar antes de cada grupo agregaría hasta 60s de overhead por job (5 grupos × 12s). Si la sesión expira entre grupos, el `publish()` falla normalmente y el error queda en `job_results`. Ver detalles en [POSTER_FLOW.md](POSTER_FLOW.md) §3.1.

### 12.12 Poll loop ya es async

`worker_main._poll_loop` ya es `async def` y se ejecuta con `asyncio.run()`. El `await orchestrator.dispatch(job)` funciona sin cambios estructurales. El dispatch es **fire-and-forget** — lanza una `asyncio.Task` y retorna. El orquestador es responsable de marcar el job como `done`/`failed` y disparar el webhook al terminar.
