# 03 — Fase 3: Refactor arquitectónico (mes 2+)

> **Objetivo:** Modernizar la base técnica para soportar crecimiento (más cuentas, más grupos, más tipos de contenido) sin deuda técnica que frene el desarrollo futuro.

> **Prerrequisito:** Fases 1 y 2 completadas y estables por 2+ semanas. No refactorizar sobre base inestable.

> **Naturaleza de esta fase:** Cambios estructurales grandes. Cada ítem vale un branch separado y revisión cuidadosa. **No hay presión de tiempo** — priorizar calidad sobre velocidad.

## Tabla de ítems

| # | Item | Prioridad | Tiempo estimado |
|---|------|-----------|-----------------|
| 3.1 | Migración a Playwright async + asyncio | 🟠 P1 | 1 semana |
| 3.2 | Flask → FastAPI con Pydantic | 🟡 P2 | 3 días |
| 3.3 | Observabilidad (structlog + Prometheus) | 🟡 P2 | 3 días |
| 3.4 | Tests unitarios + snapshots de DOM | 🟡 P2 | 1 semana |
| 3.5 | Eliminar lock global de SQLite | 🟡 P2 | 1 día |
| 3.6 | Evaluar migración Emunium → humancursor/Camoufox | 🟢 P3 | 2 días (spike) |
| 3.7 | Separar API de workers (procesos distintos) | 🟢 P3 | 3 días |

**Total estimado:** ~3-4 semanas. Distribuir en 1-2 meses.

---

## 3.1 — Migración a Playwright async + asyncio

### Problema
`sync_playwright` + `threading` tiene problemas conocidos:
- **No thread-safe oficialmente.** Funciona por accidente porque cada thread crea su propio event loop, pero Playwright oficial no lo garantiza.
- **Sin cancelación limpia.** Matar un thread que está en medio de `page.goto()` deja recursos colgados.
- **Bloqueo I/O.** Una descarga de imagen bloquea el thread completo (ver 2.9).
- **No compatible con FastAPI** sin adaptadores.

### Justificación
Playwright async permite:
- **Miles de operaciones concurrentes** en un solo event loop.
- **Cancelación con `asyncio.CancelledError`.**
- **Timeouts nativos** con `asyncio.wait_for`.
- **Integración natural** con FastAPI (siguiente ítem).
- **Patrones como `asyncio.gather`** para warmups paralelos (p.ej. cargar imagen + scroll en paralelo).

### Solución técnica

**1. Nueva estructura base:**

```python
# facebook_poster_async.py
from patchright.async_api import async_playwright, Browser, BrowserContext, Page

class FacebookPosterAsync:
    def __init__(self, account, config):
        self.account = account
        self.config = config
        self._pw = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        self.context, self.page = await self._build_browser()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def _build_browser(self):
        user_data_dir = ...
        context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            ...
        )
        page = context.pages[0] if context.pages else await context.new_page()
        return context, page

    async def login(self) -> bool:
        ...
        await self.page.goto("https://www.facebook.com/login", timeout=30000)
        email_input = self.page.locator("//input[@name='email']").first
        await email_input.wait_for(state="visible", timeout=20000)
        await self._human_click(email_input)
        await self._human_type(email_input, self.account.email)
        ...

    async def _human_type(self, locator, text: str):
        await locator.click(timeout=5000)
        kb = self.page.keyboard
        for char in text:
            if random.random() < 0.015:
                wrong = random.choice(self._TYPO_ALPHABET)
                await kb.type(wrong)
                await asyncio.sleep(random.uniform(0.15, 0.35))
                await kb.press("Backspace")
            await kb.type(char)
            await asyncio.sleep(random.lognormvariate(-2.7, 0.35))
```

**2. Uso:**
```python
async def run_job(account, config, text):
    async with FacebookPosterAsync(account, config) as poster:
        if await poster.login():
            return await poster.publish_to_all_groups(text)
    return {}
```

**3. Orquestador con `asyncio.Semaphore`:**
```python
class AsyncAccountManager:
    def __init__(self, config, max_concurrent: int = 2):
        self.sem = asyncio.Semaphore(max_concurrent)

    async def run_account(self, account, text):
        async with self.sem:
            return await run_job(account, self.config, text)

    async def run_all(self, accounts, text):
        tasks = [self.run_account(a, text) for a in accounts]
        return await asyncio.gather(*tasks, return_exceptions=True)
```

**4. Estrategia de migración:**
- No hacer big-bang. Mantener `facebook_poster.py` sync durante 2-3 semanas mientras se construye `facebook_poster_async.py` en paralelo.
- Feature flag: `USE_ASYNC_POSTER=1` en `.env` para activar por cuenta.
- Migrar cuenta por cuenta a async, validar métricas.
- Solo cuando 100% de cuentas estén en async y 2 semanas estables → eliminar código sync.

**5. Reescribir `human_browsing.py`, `gemini_commenter.py`:** todos los `time.sleep` → `await asyncio.sleep`, todos los `requests.get` → `httpx.AsyncClient.get`.

### Criterio de aceptación
- [ ] 5 cuentas corriendo concurrentemente en un solo proceso, consumiendo ~40% de CPU y ~3GB RAM (antes: 5 procesos, 80% CPU, 5GB RAM).
- [ ] Cancelación de un job en medio de `publish()` no deja Chromes zombie.
- [ ] Tests paralelos del warmup muestran que `scroll + descarga_imagen` ocurren de verdad concurrentemente.

### Riesgos
- **Complejidad de debugging:** stack traces async son menos legibles. Mitigación: logging abundante con `task_name`.
- **Dependencias que no son async:** `emunium` es sync (bloqueante). O reemplazarlo (ver 3.6) o encapsular con `asyncio.to_thread()`.
- **Aprendizaje de equipo:** si alguien no conoce asyncio, dedicar tiempo a pair programming.

---

## 3.2 — Flask → FastAPI con Pydantic

### Problema
[api_server.py](../facebook_auto_poster/api_server.py) tiene ~700 líneas con:
- Validación manual repetida (`_validate_account_input`, `_sanitize_tag`, `_extract_payload`).
- Parsing manual de JSON vs multipart en cada endpoint.
- Sin OpenAPI docs automáticas (OpenClaw tiene que adivinar el contrato).
- Sin type hints fuertes en request/response.

### Beneficios de FastAPI
- **Pydantic v2:** validación declarativa con errores descriptivos.
- **OpenAPI/Swagger UI automático** en `/docs`.
- **Async nativo** (encaja con 3.1).
- **Dependency injection** (auth, DB, rate limit como dependencies).
- **Documentación auto-generada** para OpenClaw.

### Solución técnica

**1. Modelos Pydantic:**

```python
# models.py
from pydantic import BaseModel, EmailStr, Field, validator
from datetime import datetime

class PostRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    image_path: str | None = None
    accounts: list[str] | None = None
    callback_url: str | None = None

class ScheduleRequest(PostRequest):
    scheduled_for: datetime

    @validator("scheduled_for")
    def must_be_future(cls, v):
        if v <= datetime.now():
            raise ValueError("scheduled_for debe ser futuro")
        return v

class AccountCreate(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9_]{1,30}$")
    email: EmailStr
    groups: list[str] = Field(..., min_length=1)

    @validator("groups", each_item=True)
    def group_must_be_numeric(cls, v):
        if not v.strip().isdigit():
            raise ValueError(f"ID de grupo inválido: {v}")
        return v.strip()
```

**2. Dependencias de auth:**
```python
from fastapi import Depends, HTTPException, Header

async def openclaw_auth(x_api_key: str = Header(...)):
    if not secrets.compare_digest(x_api_key, OPENCLAW_API_KEY):
        raise HTTPException(status_code=401, detail="API key inválida")

async def admin_auth(request: Request):
    if not request.session.get("admin_authenticated"):
        raise HTTPException(status_code=401)
```

**3. Endpoint con FastAPI:**
```python
from fastapi import FastAPI, Depends

app = FastAPI(title="Facebook Auto-Poster API", version="2.0")

@app.post("/post", dependencies=[Depends(openclaw_auth), Depends(rate_limit)])
async def handle_post(req: PostRequest) -> dict:
    accounts = await resolve_accounts(req.accounts)
    if not hour_allowed_for_any(accounts):
        raise HTTPException(403, detail="Fuera de horario")
    job_id = await job_store_async.create_job(...)
    asyncio.create_task(run_job(job_id, accounts, req.text, ...))
    return {"job_id": job_id, "status": "accepted"}
```

**4. Multipart para imagen:**
```python
from fastapi import UploadFile, File, Form

@app.post("/post/multipart", dependencies=[Depends(openclaw_auth)])
async def handle_post_multipart(
    text: str = Form(...),
    accounts: str | None = Form(None),
    image: UploadFile | None = File(None),
):
    ...
```

**5. Admin panel:**
FastAPI sirve HTML estático (las `publish.html`, `admin.html` actuales con Jinja2 adaptado). Alternativa: separar frontend (React/Vue) y dejar FastAPI solo con API JSON.

### Migración gradual
- Montar FastAPI en `/v2` mientras Flask sigue en `/`.
- OpenClaw puede usar ambos simultáneamente durante transición.
- Deprecar `/` en 4-6 semanas.

### Criterio de aceptación
- [ ] `GET /docs` muestra Swagger UI con todos los endpoints.
- [ ] Validación: enviar `{"text": ""}` devuelve 422 con mensaje descriptivo, no 400 genérico.
- [ ] OpenClaw puede autogenerar cliente desde `/openapi.json`.

### Riesgos
- **Sesiones:** Flask usa `flask.session`, FastAPI no trae equivalente out-of-the-box. Usar `itsdangerous` o `fastapi-sessions`.
- **Jinja2:** FastAPI lo soporta vía `Jinja2Templates` — adaptación mínima.

---

## 3.3 — Observabilidad: structlog + Prometheus

### Problema
Logs actuales son texto plano multiformato. Imposible:
- Buscar "todos los logins fallidos de elena en últimos 7 días" sin grep frágil.
- Tener métricas agregadas (tasa de éxito, latencia de publish, cola actual).
- Alertar ante degradación.

### Solución técnica

**1. Structlog (logs en JSON):**

```python
# logging_config.py
import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.dict_tracebacks,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)

logger = structlog.get_logger()
```

Uso:
```python
logger.info(
    "publish_success",
    account=self.account.name,
    group_id=group_id,
    attempt=attempt,
    duration_s=elapsed,
)
```

Log resultante (una línea JSON):
```json
{"event":"publish_success","account":"elena","group_id":"12345","attempt":1,"duration_s":12.3,"timestamp":"2026-04-23T15:30:00","level":"info"}
```

**2. Prometheus metrics:**

```python
from prometheus_client import Counter, Histogram, Gauge, start_http_server

publish_total = Counter("fb_publish_total", "Publishes attempted", ["account", "result"])
publish_duration = Histogram("fb_publish_duration_seconds", "Publish duration", ["account"])
active_workers = Gauge("fb_active_workers", "Currently active browser workers")
accounts_active = Gauge("fb_accounts_active", "Accounts available (not in cooldown)")
```

En el código:
```python
with publish_duration.labels(account=self.account.name).time():
    result = self.publish(group_id, text)
publish_total.labels(
    account=self.account.name,
    result="success" if result else "failure"
).inc()
```

Exponer en `/metrics` (FastAPI: usar `prometheus-fastapi-instrumentator`).

**3. Dashboard:**
- Grafana con datasource Prometheus.
- Paneles: tasa de éxito por cuenta (7d), latencia p50/p95/p99, cola actual, cuentas en cooldown.

**4. Alertas:**
- Tasa de éxito < 80% en 1h → alert.
- Cuenta baneada → alert inmediato.
- Cola > 50 jobs → alert.

**5. Integración con logs centralizados (opcional):**
- Loki + Promtail (stack con Grafana).
- O enviar logs a Datadog/Logflare si hay presupuesto.

### Criterio de aceptación
- [ ] Todos los `logger.info/warning/error` producen JSON.
- [ ] `curl localhost:5001/metrics` devuelve métricas Prometheus.
- [ ] Grafana dashboard muestra actividad en tiempo real.
- [ ] Query "todos los soft-bans últimas 24h" se resuelve en < 1s con Loki.

### Riesgos
- **Logs JSON más verbosos:** rotación de logs obligatoria. Configurar `logrotate` o `RotatingFileHandler`.

---

## 3.4 — Tests unitarios + snapshots de DOM

### Problema
No hay tests. Los selectores XPath de Facebook cambian cada ~1-3 semanas. Actualmente se detecta solo cuando una publicación falla en producción.

### Solución técnica

**1. Estructura:**
```
tests/
├── unit/
│   ├── test_config.py
│   ├── test_job_store.py
│   ├── test_text_variation.py
│   └── test_validators.py
├── integration/
│   ├── test_api_endpoints.py
│   └── test_scheduler.py
├── dom_snapshots/
│   ├── group_feed_20260423.html
│   ├── composer_modal_20260423.html
│   └── post_comment_20260423.html
└── conftest.py
```

**2. Tests unitarios clásicos:**

```python
# tests/unit/test_job_store.py
import pytest
from job_store import create_job, cancel_job, init_db

@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("job_store.DB_PATH", db_path)
    init_db()

def test_create_and_cancel_job(clean_db):
    job_id = create_job(text="test", accounts=None, image_path=None,
                        callback_url=None, job_type="immediate")
    assert len(job_id) == 12
    assert cancel_job(job_id) is True
    assert cancel_job(job_id) is False  # no se puede cancelar dos veces
```

**3. Snapshots de DOM (el más valioso):**

Guardar HTML reales de Facebook (sanitizados — sin datos personales) como fixtures. Los selectores XPath se testean contra esos HTMLs:

```python
# tests/integration/test_selectors.py
from pathlib import Path
from playwright.sync_api import sync_playwright

SNAPSHOTS = Path(__file__).parent.parent / "dom_snapshots"

def test_composer_selector_against_snapshot():
    html = (SNAPSHOTS / "group_feed_20260423.html").read_text()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        composer = page.locator("//span[text()='Escribe algo...']").first
        assert composer.count() > 0
        browser.close()
```

**Proceso:**
1. Cada vez que Facebook cambia el DOM y rompe producción:
   - Actualizar selector en `facebook_poster.py`.
   - Guardar snapshot nuevo con fecha.
   - Escribir test que falla con snapshot viejo y pasa con nuevo.
2. El test protege contra regresión si vuelven al DOM viejo.

**4. Tests de integración del API:**

```python
# tests/integration/test_api_endpoints.py
from fastapi.testclient import TestClient
from api_server import app

def test_post_without_auth_returns_401():
    client = TestClient(app)
    resp = client.post("/post", json={"text": "hola"})
    assert resp.status_code == 401

def test_post_with_valid_auth(monkeypatch):
    monkeypatch.setenv("OPENCLAW_API_KEY", "testkey")
    client = TestClient(app)
    resp = client.post("/post",
                       json={"text": "hola"},
                       headers={"X-API-Key": "testkey"})
    assert resp.status_code in (202, 403)  # 403 si fuera de horario
```

**5. CI:**
GitHub Actions (cuando se monte repositorio remoto):
```yaml
- run: pip install -r requirements-dev.txt
- run: pytest tests/unit -v
- run: pytest tests/integration -v --cov
- run: pytest tests/integration/test_selectors.py  # snapshots
```

### Criterio de aceptación
- [ ] Coverage > 60% en `job_store.py` y `config.py` (los más puros).
- [ ] Snapshots cubren: feed de grupo, compositor modal, área de comentarios, resultado tras publicar.
- [ ] CI corre en < 3 minutos.
- [ ] Cuando Facebook cambia un selector, hay un test que lo detecta antes de producción.

### Riesgos
- **Fragilidad de snapshots:** si el HTML tiene clases generadas aleatoriamente (React), los snapshots pueden requerir sanitización. Crear script `scrub_snapshot.py`.
- **No cubre ejecución real:** un selector puede pasar el snapshot pero fallar en Facebook real por JS que no se ejecuta. Aceptar como "primera línea" — producción sigue siendo fuente de verdad.

---

## 3.5 — Eliminar lock global de SQLite

### Problema
[job_store.py:22](../facebook_auto_poster/job_store.py#L22) define `_lock = threading.Lock()` y lo usa en **toda** operación. Con `PRAGMA journal_mode=WAL` ya habilitado, el lock es redundante y serializa inútilmente.

### Solución técnica

**1. Remover `_lock`:**
```python
# Quitar:
# _lock = threading.Lock()

# Antes:
with _lock, _connect() as conn:
    conn.execute(...)

# Después:
with _connect() as conn:
    conn.execute(...)
```

SQLite con WAL y `check_same_thread=False` maneja concurrencia correctamente a nivel de librería.

**2. Connection pooling (mejora extra):**

SQLite no requiere pooling tradicional, pero mantener una conexión por thread ayuda:
```python
import threading

_thread_local = threading.local()

def _get_conn() -> sqlite3.Connection:
    if not hasattr(_thread_local, "conn"):
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")  # espera 5s en caso de lock
        _thread_local.conn = conn
    return _thread_local.conn
```

**3. Transacciones explícitas para operaciones multi-statement:**
```python
def mark_done(job_id, results):
    conn = _get_conn()
    try:
        with conn:  # transacción implícita
            conn.execute("UPDATE jobs SET status='done' WHERE id=?", (job_id,))
            for acc, group_res in results.items():
                for gid, success in group_res.items():
                    conn.execute("INSERT INTO job_results ...", (...))
    except sqlite3.Error:
        # rollback automático
        raise
```

### Criterio de aceptación
- [ ] Benchmark: 100 concurrent `create_job()` → 5x más rápido que con lock.
- [ ] No aparece "database is locked" bajo carga (gracias a busy_timeout).

### Riesgos
- **Race conditions sutiles:** el lock actual puede estar escondiendo bugs. Correr tests existentes + nuevos de 3.4 con carga.

---

## 3.6 — Evaluar reemplazo de Emunium

### Problema
Emunium tiene mantenimiento limitado y su API es sync. Para async (3.1) necesitamos:
- Envolver cada llamada en `asyncio.to_thread()` (funciona pero costoso), **o**
- Reemplazar con algo nativo async / más moderno.

### Opciones (spike de 2 días)

#### Opción A: Mantener Emunium + `to_thread`
- **Pros:** ningún cambio funcional, riesgo bajo.
- **Contras:** cada click agrega overhead de thread-switching.

#### Opción B: **humancursor**
- **Pros:** Bézier curves nativos, integración con Playwright, mantenimiento activo.
- **Contras:** no opera a nivel OS (pero Patchright ya enmascara bien el origen).

```python
from humancursor import SystemCursor
cursor = SystemCursor()
await cursor.move_to([x, y])  # versión async
```

#### Opción C: **Camoufox** (browser completo)
- **Pros:** Firefox parcheado, anti-fingerprinting nivel superior a Chromium-patchright.
- **Contras:** cambio de motor — re-validar todos los selectores (XPath funciona, pero CSS comportamental puede variar).

#### Opción D: Bézier propio con `mouse.move` de Playwright
- **Pros:** zero dependencias nuevas, control total.
- **Contras:** reinventar la rueda.

```python
async def human_move_to(page, x, y, steps=30):
    start = await page.evaluate("() => [window.mouseX || 0, window.mouseY || 0]")
    # Bezier control points
    cp1 = (start[0] + random.randint(-50, 50), start[1] + random.randint(-50, 50))
    cp2 = (x + random.randint(-50, 50), y + random.randint(-50, 50))
    for i in range(steps):
        t = i / steps
        bx = (1-t)**3 * start[0] + 3*(1-t)**2*t * cp1[0] + 3*(1-t)*t**2 * cp2[0] + t**3 * x
        by = (1-t)**3 * start[1] + 3*(1-t)**2*t * cp1[1] + 3*(1-t)*t**2 * cp2[1] + t**3 * y
        await page.mouse.move(bx, by)
        await asyncio.sleep(random.uniform(0.005, 0.02))
```

### Metodología del spike
1. Crear branch `spike/mouse-library`.
2. Implementar las 4 opciones en una cuenta de test.
3. Medir en https://bot.sannysoft.com/ y publicar 20 veces en FB staging.
4. Comparar:
   - Tasa de detección/CAPTCHAs.
   - Latencia de click.
   - Ergonomía del código.
5. Decidir y documentar en ADR (Architecture Decision Record).

### Criterio de aceptación del spike
- [ ] Documento de decisión con tabla comparativa.
- [ ] Opción elegida con PR plan de migración.

### Riesgos
- Cambiar de motor de browser (Opción C) requiere re-validar **todo el flujo**. Alto costo.

---

## 3.7 — Separar API de workers (procesos distintos)

### Problema
Actualmente `main.py` arranca el API Flask **y** el `scheduler_runner` **y** los workers en el mismo proceso Python. Si Chrome crashea y tira el proceso → el API cae → OpenClaw ve 502.

### Solución técnica

**1. Arquitectura:**
```
┌─────────────────┐         ┌─────────────────┐
│   api_server    │ <-----> │  SQLite jobs.db │
│   (FastAPI)     │         └────────┬────────┘
└─────────────────┘                  │
                                     │
                          ┌──────────▼──────────┐
                          │   worker (N copias) │
                          │   (async poster)    │
                          └─────────────────────┘
```

- **API:** solo escribe jobs a SQLite y responde rápido. Nunca ejecuta browsers.
- **Worker(s):** procesos separados que pollean `jobs WHERE status='pending'` y ejecutan.

**2. Implementación:**

`api_main.py`:
```python
from fastapi import FastAPI
import uvicorn

app = FastAPI()
# endpoints que solo escriben a DB

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
```

`worker_main.py`:
```python
import asyncio
from worker import Worker

async def main():
    worker = Worker(worker_id=os.getenv("WORKER_ID", "0"))
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
```

Cada worker claim un job con `UPDATE jobs SET status='running', worker_id=? WHERE id=? AND status='pending'` (atómico).

**3. Orquestación:**
- **Systemd** (Linux):
  ```
  [Unit] fb-api.service
  [Unit] fb-worker@1.service
  [Unit] fb-worker@2.service
  ```
- **Windows:** `nssm` (Non-Sucking Service Manager) envuelve cada proceso.
- **Docker (fase 4 eventual):** docker-compose con servicios `api` y `worker` (escalable con `docker-compose up --scale worker=3`).

**4. Monitoreo de workers:**
Cada worker hace heartbeat cada 30s:
```sql
UPDATE workers SET last_heartbeat=NOW() WHERE id=?
```

Un watchdog detecta workers muertos (no heartbeat en 2 min) y libera sus jobs:
```sql
UPDATE jobs SET status='pending', worker_id=NULL
WHERE status='running' AND worker_id IN (SELECT id FROM workers WHERE last_heartbeat < NOW() - INTERVAL '2 min')
```

### Criterio de aceptación
- [ ] Matar un worker con `kill -9` → el job reclamado se libera en < 2 min y otro worker lo toma.
- [ ] El API responde < 50ms siempre (no bloqueado por browsers).
- [ ] Se pueden escalar workers agregando procesos sin reiniciar el API.

### Riesgos
- **Complejidad operacional:** más procesos = más piezas a monitorear.
- **Clock skew:** si workers y API están en hosts distintos, sincronizar tiempo con NTP.

---

## Orden de implementación sugerido

```
Mes 2 - Semana 1-2:
  3.1 (Playwright async)         ← base de todo lo demás async
  3.5 (Remover lock SQLite)       ← trivial, hacer en paralelo

Mes 2 - Semana 3:
  3.2 (FastAPI)                   ← depende de 3.1 para async endpoints

Mes 2 - Semana 4:
  3.3 (Observabilidad)            ← ayuda a validar todo el refactor
  3.4 (Tests)                     ← estabilidad del refactor

Mes 3 (opcional):
  3.6 (Spike mouse lib)
  3.7 (Separar API/workers)       ← solo si el volumen lo justifica
```

## Métricas de validación de fin de Fase 3

- **Throughput:** 5x más cuentas en el mismo hardware.
- **Latencia API p95:** < 100ms.
- **Coverage:** > 60% en módulos puros (config, job_store, validators).
- **MTTR** (mean time to recovery tras crash): < 1 minuto.
- **Tiempo de adaptación a cambio de DOM de FB:** < 1 hora (detectar con snapshots + fix).

---

## Consideraciones transversales

### Feature flags
Todos los refactors usan flags en `CONFIG`:
```python
"use_async_poster": False,
"use_fastapi": False,
"use_workers_process": False,
```

Permite rollback instantáneo sin code revert.

### Documentación
Cada ítem completado actualiza:
- `CLAUDE.md` (arquitectura)
- `docs/adr/NNN-decision.md` (si es decisión grande)
- Ejemplos en `/docs` de OpenAPI

### Code review
Dado el alcance de los cambios, **no auto-mergear**. Cada PR revisada por persona distinta, idealmente con ambiente de staging.
