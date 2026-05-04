# Orquestador de Sesiones — Diseño Técnico (adaptado a Fase 3 async)

> **Estado:** Especificación — pendiente de implementación por fases
> **Rama base:** `fase-3` (async-first)
> **Última revisión:** Mayo 2026
> **Reemplaza:** versión anterior basada en `produccion_temp` (sync, threading)

---

## 0. Contexto previo — qué cambió respecto a la versión anterior

Este documento es la **reescritura** del diseño original que asumía la rama `produccion_temp` (sync, `threading.Lock`, `queue.Queue`, `FacebookPoster`). El sistema actual evolucionó en Fase 3.1 a un stack async-first con:

- `FacebookPosterAsync` (no existe `FacebookPoster` sync)
- `AsyncAccountManager` con `asyncio.Semaphore` (no `multiprocessing`)
- `worker_main.py` con polling SQLite + `claim_pending_job` (CAS atómico)
- Modo `SPLIT_PROCESSES=1` para escalar API y Worker en procesos separados
- Persistencia de jobs en SQLite WAL — ningún job vive solo en memoria

**Reglas duras de esta especificación:**

1. **Cero `threading.Lock` o `queue.Queue` en código nuevo del orquestador.** Todo es `asyncio.Lock`, `asyncio.Queue`, `asyncio.Task`, `asyncio.Event`.
2. **El orquestador es una capa OPCIONAL sobre `worker_core.run_job()`.** No reemplaza `worker_main.py`. Coexiste con `SPLIT_PROCESSES`.
3. **Default OFF.** Activación con `ORCHESTRATOR_ENABLED=1`. El sistema actual sigue funcionando exactamente como hoy si la flag está apagada.
4. **Cola persistente, no en memoria.** La "cola por cuenta" es una vista SQL sobre `jobs`, no `asyncio.Queue` exclusiva.
5. **Sin multi-context Playwright** en V1 — incompatible con proxy SOCKS5 y fingerprint por cuenta. Se evalúa en Fase 2 si la RAM se vuelve limitante.
6. **Sin placeholders ni stubs.** Cada feature que se implementa, se implementa completa.

---

## 1. Motivación

El sistema sigue un patrón **fire-and-close**: cada job abre Chromium, hace login, publica en N grupos, y cierra el navegador.

```
POST /post → claim_pending_job → run_job
            → AsyncAccountManager (open → login → publish → close)
```

**Problemas:**

- **Login repetitivo**: cada job ≥ 1 login completo, lo que aumenta huella detectable
- **Sin consumo entre publicaciones**: `asyncio.sleep(30-60)` entre grupos en lugar de navegar el feed
- **Patrón de uso artificial**: la cuenta sólo "entra-publica-sale", nunca interactúa pasivamente

**Lo que añade el orquestador:**

- Pool de sesiones persistentes (Chromiums vivos entre jobs)
- Idle browsing tipificado por health score
- Consumo de feed entre grupos en vez de sleep
- Cola por cuenta (nunca dos jobs de la misma cuenta corriendo en paralelo)

---

## 2. Arquitectura

### 2.1 Diagrama lógico

```
┌──────────────────────────────────────────────────────────────────────┐
│  API (Flask)                                                         │
│  POST /post / POST /schedule / Admin /publish                        │
│         │                                                            │
│         ▼                                                            │
│  job_store.create_job()  →  SQLite (status='pending')                │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────┐       │
│  │  WORKER PROCESS (worker_main.py o monolítico)            │       │
│  │                                                          │       │
│  │  poll_loop (cada 5s):                                    │       │
│  │    job = claim_pending_job()  ← CAS SQL                  │       │
│  │    if ORCHESTRATOR_ENABLED:                              │       │
│  │       await orchestrator.dispatch(job)                   │       │
│  │    else:                                                 │       │
│  │       executor.submit(worker_core.run_job, ...)  (legacy)│       │
│  │                                                          │       │
│  │  ┌────────────────────────────────────────────────┐     │       │
│  │  │   ORCHESTRATOR (asyncio.Task daemon)           │     │       │
│  │  │                                                │     │       │
│  │  │   ┌──────────────────────────────────────┐    │     │       │
│  │  │   │   SessionPool (asyncio.Lock)         │    │     │       │
│  │  │   │   {account_name → BrowserSession}    │    │     │       │
│  │  │   │                                      │    │     │       │
│  │  │   │   tier = f(health_score)             │    │     │       │
│  │  │   │   HOT (80-100) / WARM (50-79)        │    │     │       │
│  │  │   │   COLD (0-49)                        │    │     │       │
│  │  │   └──────────────────────────────────────┘    │     │       │
│  │  │                                                │     │       │
│  │  │   idle_loop (asyncio.Task):                   │     │       │
│  │  │     for session in pool.idle_sessions():      │     │       │
│  │  │       async with _emunium_lock:               │     │       │
│  │  │         await session.do_idle_action()        │     │       │
│  │  │                                                │     │       │
│  │  │   health_check_loop (asyncio.Task):           │     │       │
│  │  │     ping cada Chromium → detectar crash       │     │       │
│  │  └────────────────────────────────────────────────┘     │       │
│  │                                                          │       │
│  │  FacebookPosterAsync × N  ← navegadores vivos persistente│       │
│  └──────────────────────────────────────────────────────────┘       │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 Convivencia con `SPLIT_PROCESSES`

| Modo | API | Worker | Orquestador |
|------|-----|--------|-------------|
| `SPLIT_PROCESSES=0` (monolítico) | Mismo proceso | Mismo proceso | Mismo proceso (asyncio.Task) |
| `SPLIT_PROCESSES=1` (separado) | Sólo encola jobs en SQLite | Corre `worker_main.py` | Vive en proceso del worker |

El orquestador **siempre** vive en el proceso que tiene Chromiums abiertos (el worker). Nunca en el API-only.

### 2.3 Máquina de estados de `BrowserSession`

```
                    ┌─────────┐
                    │ CREATED │
                    └────┬────┘
                         ▼
                ┌───────────────┐  fail
                │  LOGGING_IN   │──────┐
                └──────┬────────┘      ▼
                       ▼          ┌──────────┐
              ┌────────────────┐  │ CLOSING  │
          ┌──►│   IDLE         │  └──────────┘
          │   │ (tier=f(score))│
          │   └────┬───────────┘
          │        │ dispatch
          │        ▼
          │   ┌──────────────┐  banned/checkpoint
          │   │  PUBLISHING  │─────────────────┐
          │   └──────┬───────┘                 ▼
          │          │ done            ┌──────────────┐
          └──────────┘                 │ RESTRICTED   │
                                       │ (cooldown)   │
                                       └──────────────┘
```

Los estados `BANNED` y `COOLDOWN` del diseño original se unifican en `RESTRICTED` (driven by health_score < umbral).

**Comportamiento al detectar RESTRICTED durante publicación:**
El poster termina el grupo que está publicando en ese momento y para. No continúa con los grupos restantes del job. Los grupos no publicados se marcan como `skipped` en `job_results`. Se notifica al admin y se dispara `webhook.fire_account_banned`.

### 2.4 Modelo de navegadores y proxies

El pool tiene **hasta 4 navegadores**. Cada navegador pertenece a **una sola cuenta** durante toda la vida de la sesión. Las cuentas **nunca migran** entre navegadores.

```
Navegador 1 │ elena  │ proxy de elena  (asignado por proxy_manager)
Navegador 2 │ maria  │ proxy de maria
Navegador 3 │ (libre)│ —
Navegador 4 │ (libre)│ —
```

**Reglas de asignación:**

Si llega un job para `cuenta X`:
- `cuenta X` ya tiene sesión activa → se usa esa sesión (sin importar su estado IDLE/PUBLISHING/LOGGING_IN)
- `cuenta X` no tiene sesión, hay espacio en el pool → se crea una sesión nueva
- `cuenta X` no tiene sesión, pool lleno → job queda en `queued` hasta que se libere un navegador

Las cuentas **no migran entre navegadores** — si la sesión fue eviccionada, el próximo job abre una sesión nueva desde cero (no hereda el browser anterior).

**Evicción LRU:** sólo aplica a sesiones en estado `IDLE`. Nunca se interrumpe `PUBLISHING` o `LOGGING_IN`. Si el pool está lleno y todas las sesiones activas son `PUBLISHING` o `LOGGING_IN`, el dispatcher espera y el job queda en `queued`.

**Proxy por cuenta — proxy_manager sin cambios:**

El proxy va con la cuenta, no con el navegador. Al crear una sesión para `elena`, el orchestrator llama `proxy_manager.resolve_proxy(elena)` que retorna el proxy asignado por LRU. `proxy_manager` no se modifica — MAX_ACCOUNTS_PER_NODE=10 y asignación LRU siguen funcionando igual.

**Modo de ventanas — siempre headed:**

El orquestador corre siempre con ventanas visibles (`headless=False`). Emunium requiere escritorio activo y funciona en todas las acciones de publicación y Like. `window_layout.py` posiciona las ventanas automáticamente según el número de sesiones activas. Si en el futuro se migra a un servidor VPS sin GUI, se revisita esta decisión y se añade flag `HEADLESS`.

---

## 3. Health Score y Tiers (reemplaza rotación 4h)

### 3.1 Decisión de diseño

Los **tiers se derivan del health score**. No hay rotación programada cada 4h. Esto elimina el fallo F2 (rotación durante publicación).

```
score 80-100  → HOT     (publica con normalidad, idle ligero)
score 50-79   → WARM    (publica, idle moderado, candidato de degradación)
score 0-49    → COLD    (no publica, sólo construye confianza con idle deep)
score = 0     → RESTRICTED (cooldown 72h, sólo jobs no-publicación)
```

### 3.2 Comportamiento por tier en idle

| Comportamiento | HOT | WARM | COLD |
|----------------|-----|------|------|
| Intervalo entre acciones | 3–5 min | 1.5–4 min | 1–3 min |
| Duración por acción | 10–25 s | 20–45 s | 45–120 s |
| Scroll feed | 1–2 | 2–3 | 3–5 deep |
| Hover posts | 20% | 40% | 60% |
| Abrir comentarios | — | 20% | 40% |
| Comentar (Gemini) | — | 10% | 15% |
| Reaccionar (Like) | — | — | 30% |
| Ver videos | — | — | 25% |
| Usa Emunium | sólo al publicar | no | no |

**Las acciones idle de WARM y COLD usan Playwright puro (no Emunium)** — esto permite N sesiones idle simultáneas sin conflicto de cursor del SO. Sólo HOT publicando o haciendo "Like" usa Emunium (rediseño F9).

### 3.3 Eventos que mueven el score

| Evento | Δ score | Notas |
|--------|---------|-------|
| Login OK | +1 | Capped al máximo |
| Publicación OK | +2 | Sólo si grupo confirma post-id |
| Publicación falla por timeout | -3 | |
| Post desaparece tras N min (verificación) | -15 | Indicador de soft-ban |
| Checkpoint detectado | -25 | Llega a `RESTRICTED` rápido |
| Login redirige a `checkpoint` o `login` | -10 | Sesión expirada |
| 24h sin publicar exitoso | -1 | Decay natural |
| Cooldown completo (72h sin nuevos eventos negativos) | reset → 60 | Vuelve a WARM |

Todos los eventos se registran en `account_health_events` con timestamp y delta. El score se actualiza con `UPDATE accounts SET health_score = MAX(0, MIN(100, health_score + ?)) WHERE name = ?` (atomic, cubre F11).

---

## 4. Cola por cuenta (rediseño F4)

### 4.1 Concepto

Cada cuenta tiene su propia cola lógica. Se garantiza que **nunca hay dos jobs de la misma cuenta ejecutándose en paralelo** — esto evita el escenario de "dos navegadores de la misma cuenta activos al mismo tiempo" que dispararía detección de Facebook.

### 4.2 Implementación — vista SQL, no `asyncio.Queue`

La cola por cuenta no es una estructura en memoria. Es una **vista lógica** sobre la tabla `jobs` filtrada por cuenta:

```sql
-- "Próximo job para una cuenta" — usado por el dispatcher del orquestador
SELECT j.* FROM jobs j
WHERE j.status = 'pending'
  AND (j.accounts IS NULL OR j.accounts LIKE '%"' || ? || '"%')
  AND NOT EXISTS (
    SELECT 1 FROM jobs running
    WHERE running.status = 'running'
      AND running.accounts LIKE '%"' || ? || '"%'
  )
ORDER BY j.created_at ASC
LIMIT 1
```

Si la cuenta ya tiene un job en `running`, no se reclama otro. Esto cubre F4 sin perder persistencia: si el proceso muere, los jobs siguen en `pending` y se retoman al reiniciar.

### 4.3 Detección de duplicados

Antes de encolar un job, el dispatcher verifica si ya existe un job pendiente o running que publique en el mismo grupo.

**Caso A — misma cuenta + mismo grupo:**
```
Job existente: elena → grupo "ventas" (pendiente o running)
Job nuevo:     elena → grupo "ventas"
→ Se DESCARTA el job nuevo. Notificación al admin.
```

**Caso B — mismo grupo, diferente cuenta:**
```
Job existente: elena → grupo "ventas" (pendiente o running)
Job nuevo:     maria → grupo "ventas"
→ Se AVISA al admin: "elena tiene pendiente publicar en 'ventas' a las HH:MM"
→ El job de maria se ENCOLA normalmente — no se descarta.
```

**Caso C — misma cuenta, diferente grupo:**
```
Job existente: elena → grupo "ventas"
Job nuevo:     elena → grupo "deportes"
→ Se encola en la cola de elena sin notificación. No es duplicado.
```

### 4.4 Modelo de bloques — agenda del orquestador

El orquestador no gestiona jobs uno por uno en tiempo real. Trabaja con **bloques de tiempo** — agrupaciones de publicaciones que ocurren en una ventana horaria.

```
Bloque 8:30:
  elena  (HOT)  → publica
  maria  (WARM) → publica
  pedro  (COLD) → consume contenido en navegador libre
  juan   (RESTR)→ sin sesión, cooldown activo

  Duración estimada = max_grupos × (wait_between_groups_max + consume_between_groups_max)
```

**Jobs on-demand** (sin hora programada) se insertan en el **próximo bloque disponible** dentro de `active_hours`. El orquestador calcula si el job cabe en el tiempo restante del bloque actual o va al siguiente.

**Respuesta al admin cuando pide un slot:**
- `"Tengo espacio"` — hay hueco en el bloque actual o el próximo
- `"Tengo todo lleno"` — todos los bloques dentro de `active_hours` están completos
- `"Tengo un espacio vacío a las HH:MM"` — próximo bloque con capacidad disponible

**Cuentas COLD en bloques:**
- Se excluyen de publicar → admin recibe notificación
- Se asignan al navegador libre para consumo de contenido (idle browsing)
- El orquestador prioriza la cuenta COLD con score más cercano a 50 (la que está a punto de recuperarse)
- Cuando cruza a WARM, el admin recibe: `"pedro recuperó WARM — disponible para publicar"`

### 4.5 Jobs en espera — sistema lleno (estado `queued`)

Cuando todos los navegadores están ocupados (todos en `PUBLISHING` o `LOGGING_IN`):

```
Dispatcher detecta: sin navegadores disponibles
        ↓
Job queda en estado 'queued' (no 'running', no 'pending')
        ↓
Notificación al admin: "Sistema lleno — job X en espera"
        ↓
Cuando se desocupa un navegador:
  ¿La cuenta está dentro de active_hours?
    Sí → ejecuta inmediatamente
    No → espera al siguiente día dentro de active_hours
```

**Estado `queued`:** el job no se marca como `running` hasta que el orquestador tiene un navegador disponible Y la cuenta está en horario activo. Esto evita jobs huérfanos si el proceso muere mientras espera.

**TTL de `queued`:** configurable via `QUEUED_TIMEOUT_HOURS` (default 4h). Si el job lleva más de `QUEUED_TIMEOUT_HOURS` en `queued` sin poder arrancar, vuelve a `pending` y el dispatcher lo reintenta en el próximo ciclo. El admin recibe aviso: `"Job X devuelto a pending — sistema ocupado por más de Xh"`.

### 4.6 Cuentas restringidas — cancelación y notificación

```
Job programado → cuenta = RESTRICTED
        ↓
Health score cancela todos los jobs pendientes de esa cuenta
        ↓
Notificación al admin: "Cuenta X restringida — N jobs cancelados"
Webhook: fire_account_banned() (ya existe)
```

**Durante cooldown la cuenta sí puede ejecutar:**
- Discovery de grupos (`group_discoverer`)
- Scroll y hover de feed (Playwright puro)
- Reaccionar con Like (Emunium) — interacción normal, no causa restricción
- Comentar con Gemini — ídem
- Todo excepto `publish_to_all_groups`

**Razonamiento:** las restricciones se originan por volumen de publicaciones, no por interacciones sociales. Like y comentar son comportamiento normal de usuario y ayudan a recuperar el health score más rápido.

---

## 5. Componentes nuevos y modificaciones

### 5.0 Estructura de archivos — un archivo por responsabilidad

`orchestrator_async.py` solo coordina — llama funciones de módulos externos pero no implementa lógica propia. Si una función tiene más de ~20 líneas de lógica interna, va a su propio archivo.

| Archivo | Responsabilidad |
|---------|----------------|
| `orchestrator_async.py` | Coordinator: `SessionPool`, `BrowserSession`, `Orchestrator` |
| `window_layout.py` | NUEVO: calcula posición y tamaño de ventanas según navegadores activos |
| `account_health.py` | NUEVO (Fase A): health score, eventos, tiers |
| `session_dispatch.py` | NUEVO (Fase D): dispatch, duplicados, estimación de tiempo, cola de espera |

Esta regla aplica a todo lo que se implemente en el orquestador — ningún archivo crece sin límite.

### 5.0.1 `window_layout.py` — NUEVO

Calcula y aplica el layout de ventanas según el número de navegadores activos en el pool. El orquestador llama `apply_layout(sessions)` cada vez que crea o elimina una sesión.

```
1 navegador  → ventana completa (100%)
2 navegadores → mitad izquierda + mitad derecha (50% cada uno)
3 navegadores → dos columnas izquierda (25% c/u) + columna derecha completa (50%)
4 navegadores → cuadrícula 2×2 (25% cada uno)
```

```python
# window_layout.py
def apply_layout(sessions: list[BrowserSession]) -> None:
    """Recalcula y reposiciona todas las ventanas según cuántas sesiones hay."""
    layouts = _get_layout(len(sessions))  # retorna lista de (x, y, w, h) normalizados
    for session, (x, y, w, h) in zip(sessions, layouts):
        session.poster.set_viewport(x, y, w, h)
```

**Emunium usa estas coordenadas** para calcular clics absolutos en pantalla. Al tener posiciones fijas y conocidas, los clics siempre van al lugar correcto aunque haya múltiples ventanas.

**Cuándo se llama `apply_layout`:**
- Al crear una nueva sesión en el pool
- Al eliminar una sesión del pool (evicción o cierre)
- Al arrancar el orquestador con sesiones ya existentes

### 5.1 `account_health.py` — NUEVO (Fase A)

```python
class AccountHealthManager:
    """Gestiona health score de cuentas. Standalone — no depende del orquestador."""

    async def record_event(self, account: str, event_type: str, delta: int, context: str = "") -> int:
        """Registra evento + actualiza score atómicamente. Retorna nuevo score."""

    async def get_score(self, account: str) -> int: ...
    async def get_tier(self, account: str) -> str:  # "HOT" | "WARM" | "COLD" | "RESTRICTED"
        ...
    async def is_restricted(self, account: str) -> bool: ...
    async def reset_after_cooldown(self, account: str) -> bool: ...
```

Uso desde el poster (Fase B+):

```python
# En FacebookPosterAsync.publish()
if not await self._verify_post_visible(post_id, after_seconds=120):
    await health.record_event(self.account.name, "post_disappeared", -15)
```

### 5.2 `orchestrator_async.py` — NUEVO (Fase D+)

Archivo separado para no romper imports actuales.

```python
class SessionState(StrEnum):
    CREATED = "created"
    LOGGING_IN = "logging_in"
    IDLE = "idle"
    PUBLISHING = "publishing"
    RESTRICTED = "restricted"
    CLOSING = "closing"


class BrowserSession:
    """Wrapper async sobre FacebookPosterAsync con estado y locks asyncio."""

    account: AccountConfig
    state: SessionState
    poster: FacebookPosterAsync | None
    last_activity: datetime
    last_idle_action: datetime | None
    _job_lock: asyncio.Lock         # ← asyncio, no threading

    async def login(self) -> bool: ...
    async def publish_job(self, job: dict) -> dict: ...
    async def do_idle_action(self) -> None: ...
    async def close(self, really_close: bool = True) -> None: ...
    async def health_ping(self) -> bool:  # F5 — detecta crash Chromium
        try:
            await self.poster.page.title()
            return True
        except Exception:
            return False


class SessionPool:
    sessions: dict[str, BrowserSession]
    _lock: asyncio.Lock
    _emunium_lock: asyncio.Lock     # ← global, F9

    async def get(self, account_name: str) -> BrowserSession | None: ...
    async def create_session(self, account: AccountConfig) -> BrowserSession: ...
    async def evict_idle_lru(self) -> BrowserSession | None:
        # F3: nunca evicciona PUBLISHING/LOGGING_IN
        # Si no hay candidatos IDLE, retorna None — caller debe esperar
        ...
    def total(self) -> int: ...
    def can_grow(self) -> bool: ...


class Orchestrator:
    pool: SessionPool
    health: AccountHealthManager
    _stop_event: asyncio.Event
    _tasks: list[asyncio.Task]      # idle_loop, health_check_loop

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def dispatch(self, job: dict) -> None:
        """Llamado por el worker poll_loop cuando reclama un job pendiente.
        Si la cuenta no tiene sesión: crea o espera espacio.
        Si ya tiene sesión IDLE: la usa.
        Si ya tiene sesión PUBLISHING: re-encola (no crea segunda)."""
```

### 5.3 `facebook_poster_async.py` — modificaciones (Fases B y C)

| Método nuevo | Fase | Descripción |
|--------------|------|-------------|
| `consume_feed_async(min_s, max_s)` | B | Scroll + hover + peek_comments en feed principal |
| `consume_feed_cold_async(min_s, max_s)` | B | Como `consume_feed` + reaccionar + ver videos |
| `consume_group_feed_async(group_id, min_s, max_s)` | B | Pre-publicación: navega al grupo y observa antes de postear |
| `_react_to_random_post_async()` | B | Click "Me gusta" con Emunium |
| `_watch_random_video_async()` | B | Scroll a video + sleep 8-25s |
| `_check_for_checkpoint_async()` | B | F8 — verifica URL después de cada acción de consumo |

| Modificación | Fase | Descripción |
|--------------|------|-------------|
| `close(really_close: bool = True)` | C | `False` mantiene Chromium vivo, sólo limpia loggers/handlers |
| `verify_session_async()` | C | F6 — verifica que sigue logueado, redirige a `LOGGING_IN` si no |

**Lo que NO se cambia del poster:** `login()`, `publish_to_all_groups()`, `_is_logged_in()`. Existen y funcionan; el orquestador las consume tal cual.

### 5.4 `worker_main.py` — branch condicional (Fase D)

`worker_core.run_job` (legacy) se mantiene intacto. Cuando `ORCHESTRATOR_ENABLED=1`, tanto los jobs inmediatos como los jobs programados van al orquestador — sin tocar `scheduler_runner.py`.

**Jobs inmediatos** (`_poll_loop`):

```python
# en worker_main._poll_loop
if CONFIG.get("orchestrator_enabled"):
    await orchestrator.dispatch(job_dict)
else:
    executor.submit(worker_core.run_job, ...)  # ruta actual
```

**Jobs programados** (`scheduler_runner`):

`scheduler_runner._loop()` llama `dispatch_fn(_run_scheduled_job, job)`. Cuando el orquestador está activo, `worker_main` pasa una función que ignora `_run_scheduled_job` y despacha directamente al orquestador:

```python
# worker_main.main() — Fase D
if CONFIG.get("orchestrator_enabled"):
    def _scheduled_via_orchestrator(fn, job):
        # fn = _run_scheduled_job — ignorado. El orquestador gestiona el job.
        asyncio.run_coroutine_threadsafe(orchestrator.dispatch(job), loop)
    scheduler_runner.start(dispatch_fn=_scheduled_via_orchestrator)
else:
    scheduler_runner.start(dispatch_fn=executor.submit)  # comportamiento actual
```

`loop` es el event loop principal, capturado en el closure al crear el orquestador (Fase D gestiona el loop explícitamente). `scheduler_runner.py` no se modifica.

### 5.5 `api_server.py` — endpoints nuevos (Fase F)

| Método | Path | Descripción |
|--------|------|-------------|
| GET | `/admin/api/orchestrator/status` | Estado del pool (sesiones, tiers, scores) |
| GET | `/admin/api/accounts/<name>/health` | Health score + últimos eventos de la cuenta |
| POST | `/admin/api/accounts/<name>/health/reset` | Reset manual del score (admin only) |

### 5.6 `config.py` — flags nuevas

```python
"orchestrator_enabled":     os.getenv("ORCHESTRATOR_ENABLED", "0").strip() == "1",
"pool_max_total":           int(os.getenv("POOL_MAX_TOTAL", "4")),
"session_max_idle_seconds": int(os.getenv("SESSION_MAX_IDLE_SECONDS", "1800")),
"queued_timeout_hours":     int(os.getenv("QUEUED_TIMEOUT_HOURS", "4")),
"consume_between_groups_min": int(os.getenv("CONSUME_BETWEEN_GROUPS_MIN", "120")),
"consume_between_groups_max": int(os.getenv("CONSUME_BETWEEN_GROUPS_MAX", "300")),
"consume_group_feed_min":   int(os.getenv("CONSUME_GROUP_FEED_MIN", "15")),
"consume_group_feed_max":   int(os.getenv("CONSUME_GROUP_FEED_MAX", "45")),
# Tiers e idle: hard-coded en config.py (no env), porque son comportamiento, no infra
```

### 5.7 `job_store.py` — schema (Fase A)

```sql
ALTER TABLE accounts ADD COLUMN health_score INTEGER NOT NULL DEFAULT 80;
ALTER TABLE accounts ADD COLUMN restricted_until TEXT;  -- ISO timestamp o NULL

-- Estado 'queued': job reclamado por el orquestador pero esperando navegador o active_hours
-- Flujo: pending → queued → running → done/failed
-- Sin orquestador: pending → running (flujo original sin cambios)
ALTER TABLE jobs ADD COLUMN queued_at TEXT;  -- timestamp cuando entró a queued, NULL si no aplica

CREATE TABLE IF NOT EXISTS account_health_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name  TEXT NOT NULL,
    event_type    TEXT NOT NULL,         -- 'post_disappeared', 'checkpoint', etc.
    delta         INTEGER NOT NULL,
    new_score     INTEGER NOT NULL,
    context       TEXT,                  -- JSON libre
    detected_at   TEXT NOT NULL,
    FOREIGN KEY (account_name) REFERENCES accounts(name) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_health_events_account
    ON account_health_events(account_name, detected_at DESC);
```

`account_bans` se mantiene para compatibilidad histórica pero se deprecia: nuevos eventos van a `account_health_events`. Migración: opcional, no se reescribe lo histórico.

---

## 6. Plan de fases — todas independientes

> Cada fase aporta valor sola. Si una falla en testing, las anteriores siguen vivas y útiles. El orquestador completo (D+E+F) sólo se activa con `ORCHESTRATOR_ENABLED=1`; mientras tanto Fases A y B mejoran el sistema actual.

### Fase A — Health score (BAJO riesgo, 1-2 días)

| Item | Archivo |
|------|---------|
| Schema: `health_score`, `restricted_until`, `account_health_events` | `job_store.py` |
| `AccountHealthManager` standalone | `account_health.py` (nuevo) |
| Endpoint `GET /admin/api/accounts/<name>/health` | `api_server.py` |
| Tests unitarios: delta capped, restricted detection | `tests/test_health.py` (nuevo) |
| Hook en `FacebookPosterAsync.login()`: `+1 / -10` | `facebook_poster_async.py` |
| Hook en `FacebookPosterAsync.publish()`: `+2 / -3` | `facebook_poster_async.py` |

**Default:** ON (read-only para empezar — los hooks suman/restan pero el score no afecta a nada todavía).

**Valor independiente:** sustituye el ban binario actual con degradación gradual visible en el panel admin. Útil incluso si nunca se activa el orquestador.

### Fase B — Idle/consume browsing en poster (MEDIO riesgo, 2-3 días)

| Item | Archivo |
|------|---------|
| `consume_feed_async`, `consume_feed_cold_async`, `consume_group_feed_async` | `facebook_poster_async.py` |
| `_react_to_random_post_async`, `_watch_random_video_async` | `facebook_poster_async.py` |
| F8: `_check_for_checkpoint_async` después de cada acción de consumo | `facebook_poster_async.py` |
| Reuso de helpers de `human_browsing.py` | `human_browsing.py` (read-only) |
| Flag `CONSUME_BETWEEN_GROUPS_MIN/MAX` | `config.py` + `.env.example` |
| Hook opcional en `publish_to_all_groups`: si flag activa, reemplaza `sleep` por `consume_feed_async` | `facebook_poster_async.py` |

**Default:** OFF.

**Valor independiente:** mejora el patrón humano entre grupos sin necesitar pool.

### Fase C — Sesión persistente opcional (ALTO riesgo, 3-5 días)

| Item | Archivo |
|------|---------|
| `close(really_close: bool = True)` | `facebook_poster_async.py` |
| `verify_session_async()` | `facebook_poster_async.py` |
| F5: health-ping del Chromium | `facebook_poster_async.py` |
| F6: re-verificar login antes de cada publish | `facebook_poster_async.py` |
| F7: estado CLOSING si login falla | `facebook_poster_async.py` |
| Tests E2E con sesión persistida entre dos jobs consecutivos del mismo `run_job` | `tests/test_persistent_session.py` |

**Default:** OFF.

**Valor independiente:** un solo `run_job` con varias cuentas reutiliza login si la flag está ON, aún sin pool global.

### Fase D — Pool async + cola por cuenta (ALTO riesgo, 1 semana)

| Item | Archivo |
|------|---------|
| `BrowserSession`, `SessionPool`, `Orchestrator` | `orchestrator_async.py` (nuevo) |
| F9: `_emunium_lock` global | `orchestrator_async.py` |
| F4: cola por cuenta vía SQL en `claim_pending_job_for_account()` | `job_store.py` |
| F3: `evict_idle_lru` con espera activa (no fuerza) | `orchestrator_async.py` |
| Branch en `worker_main._poll_loop` | `worker_main.py` |
| Tests integración: pool lleno, evicción, dispatch concurrente | `tests/test_orchestrator.py` |

**Default:** OFF (`ORCHESTRATOR_ENABLED=0`).

**Valor independiente:** sesiones reutilizadas entre jobs distintos para la misma cuenta.

### Fase E — Tiers derivados + idle scheduling (MEDIO riesgo, 3-5 días)

| Item | Archivo |
|------|---------|
| `tier_for_score()` helper | `account_health.py` |
| `_idle_loop` task daemon en orchestrator | `orchestrator_async.py` |
| Comportamiento por tier (tabla §3.2) | `orchestrator_async.py` |
| Tests: idle action no se dispara durante PUBLISHING (F1) | `tests/test_orchestrator.py` |

**Default:** OFF (incluido en `ORCHESTRATOR_ENABLED`).

**Valor independiente:** con pool activo + tiers, las sesiones idle hacen browsing autónomo entre jobs.

### Fase F — Integración admin + observabilidad (BAJO riesgo, 2 días)

| Item | Archivo |
|------|---------|
| `GET /admin/api/orchestrator/status` | `api_server.py` |
| Sección en `templates/admin.html` con vista del pool | `admin.html` |
| Métricas Prometheus: `orchestrator_pool_size`, `orchestrator_idle_actions_total`, `orchestrator_evictions_total` | `metrics.py` |
| Notificación dual en `RESTRICTED`: badge admin + `webhook.fire_account_banned` (ya existe) | `orchestrator_async.py` |

**Total estimado:** 4-6 semanas de trabajo dedicado.

---

## 7. Consideraciones técnicas

### 7.1 Async vs threading

**Regla:** todo el código nuevo del orquestador es asyncio. Los `account_locks` actuales en `worker_core.py` (threading.Lock) **no se usan** cuando `ORCHESTRATOR_ENABLED=1` — la cola por cuenta SQL los reemplaza.

Cuando `ORCHESTRATOR_ENABLED=0`, `worker_core.run_job` (legacy con threading) sigue funcionando exactamente como hoy. Los dos modos son mutuamente excluyentes — nunca corren a la vez.

### 7.2 Persistencia ante crash

| Escenario | Comportamiento |
|-----------|----------------|
| Worker muere con jobs en `PUBLISHING` | `mark_running_as_interrupted()` al reiniciar (ya existe) |
| Sesión Chromium crashea (F5) | `health_ping()` detecta → marca sesión `CLOSING`, jobs en curso → `failed` |
| Worker muere con jobs en `queued` | Al reiniciar, jobs `queued` vuelven a `pending` — el orquestador los reclama de nuevo |
| Job en espera por `active_hours` | Permanece en `queued` — seguro ante crash, se retoma al reiniciar dentro de horario |
| `kill -9` | Chromiums quedan huérfanos en SO; cleanup al arranque (ver §7.4) |

**Flujo correcto de estados de un job con orquestador:**
```
pending → (dispatcher verifica disponibilidad) → queued → (navegador libre + active_hours) → running → done/failed
```
El job nunca pasa de `pending` a `running` directamente cuando el orquestador está activo.

### 7.3 Recursos

- 4 Chromiums simultáneos × ~400 MB = ~1.6 GB RAM
- Proxy por cuenta vía `proxy_manager` (hasta 10 cuentas por nodo, comportamiento actual sin cambios)
- CPU: picos durante publish + idle action; mayormente quieto

### 7.4 Arranque limpio

Al iniciar el worker con orquestador activo:

1. `mark_running_as_interrupted()` (ya existe)
2. `cleanup_orphan_chromiums()` (nuevo, Fase D) — mata cualquier proceso `chrome.exe` con working dir en `browser_profiles/` del proyecto
3. Sesiones se crean según demanda del modelo de bloques — las cuentas con publicaciones más tempranas en el día arrancan primero. No hay escalonamiento fijo.

### 7.5 Graceful shutdown

```python
# orchestrator_async.stop()
self._stop_event.set()
await asyncio.gather(*self._tasks, return_exceptions=True)
async with self.pool._lock:
    for session in list(self.pool.sessions.values()):
        await session.close(really_close=True)
```

Llamado desde el handler SIGTERM/SIGINT de `worker_main.py` antes de `mark_running_as_interrupted()`.

---

## 8. Lo que NO incluye este diseño (rechazado o postergado)

| Item | Razón |
|------|-------|
| Multi-context Playwright (1 Chromium con N contexts) | Incompatible con proxy SOCKS5 y fingerprint únicos por cuenta. Re-evaluar si RAM > 16GB |
| Rotación COLD↔WARM cada 4h | Eliminada — tier deriva del health score (rediseño F2) |
| `queue.Queue` thread-safe | Reemplazada por SQL `claim_pending_job_for_account` (rediseño F4) |
| `_publish_social_post()` placeholder | Política del proyecto: nada de stubs sin implementación |
| `account_bans` deprecated | Se mantiene como tabla legacy; no se migra histórico |
| Perfiles de Chrome persistentes (`CHROME_PROFILE_PATH`) | Ya removido de `.env.example` — usar `account_cookies` (DB) sigue siendo la fuente |
| Reescritura de `worker_main.py` | Se extiende con un branch, no se reemplaza |

---

## 9. Tests y verificación

### 9.1 Unitarios

| Test | Fase | Qué verifica |
|------|------|--------------|
| `test_health_score_capped` | A | Score nunca pasa de 100 ni baja de 0 |
| `test_health_score_atomic_update` | A | UPDATE single-statement, no race |
| `test_tier_derivation` | A | Score 80 → HOT, 50 → WARM, 30 → COLD, 0 → RESTRICTED |
| `test_consume_feed_respects_duration` | B | `consume_feed_async(10, 20)` siempre 10-20s |
| `test_checkpoint_during_consume` | B | F8 dispara excepción correcta |
| `test_pool_evict_excludes_publishing` | D | F3 — nunca eviccionar PUBLISHING |
| `test_emunium_lock_serialises` | D | F9 — dos sesiones no usan Emunium a la vez |
| `test_account_queue_no_parallel` | D | F4 — dos jobs misma cuenta no corren simultáneo |
| `test_idle_skipped_during_publish` | E | F1 — idle no se dispara si la sesión está PUBLISHING |

### 9.2 Integración

- Orquestador OFF → comportamiento idéntico al actual (no rompe nada)
- Orquestador ON, 1 cuenta, 2 jobs consecutivos → segundo job reutiliza sesión
- Orquestador ON, pool lleno → nuevo job espera (no fuerza evicción)
- `kill -9` durante publish → siguiente arranque limpia y marca `interrupted`

### 9.3 Manual antes de promover ON en producción

```bash
ORCHESTRATOR_ENABLED=1 python facebook_auto_poster/worker_main.py
# en otra terminal:
curl -X POST http://localhost:5000/post -H "X-API-Key: ..." -d '{"text":"test","accounts":["cuenta_real_test"]}'
sleep 60
curl http://localhost:5000/admin/api/orchestrator/status -b cookies.txt
# verificar que sesión sigue IDLE, no se cerró
```

---

## 10. Logs

```python
logger = logging.getLogger("orchestrator")  # logs/orchestrator.log
```

Eventos:

- `INFO`: session created, login OK, dispatch, idle action ejecutada, eviction
- `WARNING`: pool lleno → espera, login falla → cuenta a RESTRICTED, sesión expirada
- `ERROR`: crash de Chromium, excepción no manejada en idle_loop
- `DEBUG`: cada tick del idle_loop, cada pool snapshot

---

## 11. Roadmap

| Fase | Descripción | Estado |
|------|-------------|--------|
| A | Health score | 🔴 No iniciada |
| B | Consume browsing | 🔴 No iniciada |
| C | Sesión persistente | 🔴 No iniciada |
| D | Pool + cola por cuenta | 🔴 No iniciada |
| E | Tiers + idle scheduling | 🔴 No iniciada |
| F | Admin + observabilidad | 🔴 No iniciada |
| Futuro | Multi-context (re-evaluar) | 🟡 Pospuesto |
| Futuro | Pool dinámico ajustado a carga | 🟢 Baja prioridad |
| Futuro | Posts sociales generativos en COLD | 🟢 Baja prioridad |
