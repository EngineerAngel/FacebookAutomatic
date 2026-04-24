# 02 — Fase 2: Hardening (semanas 2-3)

> **Objetivo:** Persistir identidad completa (browser state), hacer el sistema robusto bajo carga, y dejar producción estable sin hacks.

> **Prerrequisito:** Fase 1 completada y validada con métricas en verde. Si hay soft-bans, no avanzar hasta estabilizar.

## Tabla de ítems

| # | Item | Prioridad | Tiempo estimado |
|---|------|-----------|-----------------|
| 2.1 | `user_data_dir` persistente por cuenta | 🔴 P0 | 1 día |
| 2.2 | Variación real de texto con Gemini (parafraseo) | 🟠 P1 | 1.5 días |
| 2.3 | Pool de workers con límite de concurrencia | 🟠 P1 | 1.5 días |
| 2.4 | Servidor de producción (waitress) | 🟠 P1 | 0.5 días |
| 2.5 | Pin de dependencias + auditoría | 🟡 P2 | 0.5 días |
| 2.6 | Rate limiter persistente (SQLite) | 🟡 P2 | 0.5 días |
| 2.7 | Desactivación automática post-ban | 🟠 P1 | 0.5 días |
| 2.8 | Healthcheck endpoint | 🟡 P2 | 0.5 días |
| 2.9 | Descarga de imágenes no bloqueante | 🟡 P2 | 0.5 días |

**Total estimado:** ~7 días hábiles (distribuidos en 2 semanas).

---

## 2.1 — `user_data_dir` persistente por cuenta

### Problema
Actualmente [facebook_poster.py:186](../facebook_auto_poster/facebook_poster.py#L186) usa `browser.launch()` + `browser.new_context()` sin perfil persistente. Cada sesión arranca "limpia": sin localStorage, sin IndexedDB, sin service workers cacheados, sin preferencias de Facebook (timeline, idioma de UI, notificaciones descartadas).

Un usuario real **acumula estado** en su browser. Una sesión siempre "virgen" es señal de browser automation incluso con cookies restauradas.

### Solución técnica

**1. Migrar a `launch_persistent_context`:**

```python
from pathlib import Path

def _build_browser(self) -> tuple[BrowserContext, Page]:
    user_data_dir = Path(__file__).resolve().parent / "browser_profiles" / self.account.name
    user_data_dir.mkdir(parents=True, exist_ok=True)

    fp = self.account.fingerprint  # de Fase 1.3
    w, h = fp["viewport"]

    args = [
        f"--window-position={self._window_x_offset},{self._window_y_offset}",
        f"--window-size={w},{h}",
        "--disable-notifications",
        f"--lang={fp['locale'].split('-')[0]}",
        "--disable-blink-features=AutomationControlled",
    ]

    launch_kwargs = dict(
        user_data_dir=str(user_data_dir),
        headless=self.config["browser_headless"],
        args=args,
        user_agent=fp["user_agent"],
        viewport={"width": w, "height": h},
        locale=fp["locale"],
        timezone_id=fp["timezone"],
        color_scheme=fp["color_scheme"],
    )
    if self.account.proxy:
        launch_kwargs["proxy"] = self.account.proxy

    context = self._pw.chromium.launch_persistent_context(**launch_kwargs)
    page = context.pages[0] if context.pages else context.new_page()
    return context, page
```

El `browser` desaparece — `launch_persistent_context` devuelve el context directamente. Ajustar `close()` y referencias.

**2. Estructura de directorios:**
```
facebook_auto_poster/
└── browser_profiles/       (gitignored)
    ├── maria/
    ├── elena/
    └── zofia/
```

**3. Ajustar `.gitignore`:**
```
browser_profiles/
```

**4. Migración de cookies existentes:**
La primera vez que una cuenta use el nuevo sistema, el profile está vacío. Inyectar las cookies guardadas en SQLite una sola vez → a partir de ahí el profile persiste todo.

**5. Cleanup periódico:**
Los profiles crecen (~200-500MB por cuenta). Script mensual que limpia `Cache/`, `Service Worker/CacheStorage/` conservando cookies y localStorage.

### Criterio de aceptación
- [ ] Arrancar una cuenta, cerrar, reabrir → Facebook recuerda preferencias de idioma/dark mode.
- [ ] localStorage persiste entre sesiones.
- [ ] Canvas fingerprint se mantiene consistente por cuenta entre arranques (crítico: no debe variar dentro de la misma cuenta).
- [ ] Las cookies en `jobs.db` siguen siendo backup, pero el flujo principal usa el profile.

### Riesgos
- **Tamaño de disco:** calcular ~500MB × 10 cuentas = 5GB. Monitorear y limpiar periódicamente.
- **Corrupción de profile:** si Chrome crashea, el profile puede quedar corrupto. Añadir detección (archivo `LOCK` stale) y fallback: renombrar profile a `{name}.corrupt.{timestamp}` y arrancar limpio.
- **Incompatibilidad con modo parallel:** dos procesos abriendo el mismo `user_data_dir` → crash. Serializar por cuenta (un lock file).

---

## 2.2 — Variación real de texto con Gemini

### Problema
[facebook_poster.py:45-65](../facebook_auto_poster/facebook_poster.py#L45-L65) usa solo zero-width chars + espacios no-break. Facebook tokeniza el texto (remueve whitespace/invisible chars) y hashea el canonical form → detecta texto idéntico entre grupos.

### Solución técnica

**1. Nuevo módulo `text_variation.py`:**

```python
"""Parafrasea el texto del anuncio manteniendo intención publicitaria."""
import hashlib
from gemini_commenter import GeminiCommenter

class TextVariator:
    def __init__(self, gemini: GeminiCommenter, cache_enabled: bool = True):
        self.gemini = gemini
        self.cache: dict[str, str] = {}
        self.cache_enabled = cache_enabled

    def variate(self, text: str, account_name: str, group_id: str) -> str:
        """Devuelve una versión parafraseada. Cached por (text_hash, account, group)."""
        key = self._cache_key(text, account_name, group_id)
        if self.cache_enabled and key in self.cache:
            return self.cache[key]

        prompt = f"""Parafrasea el siguiente anuncio manteniendo:
- Intención comercial exacta
- Emojis (si hay)
- URLs (no modificar)
- Números de teléfono/precios (no modificar)
- Longitud aproximada (±20%)

Varía: sinónimos, orden de frases, ganchos iniciales, tono ligeramente.

NO uses prefijos tipo "Aquí está:" ni comillas. Devuelve SOLO el texto parafraseado.

TEXTO ORIGINAL:
{text}"""

        try:
            result = self.gemini.generate_text(prompt, max_tokens=600)
            if result and len(result.strip()) > 20:
                self.cache[key] = result.strip()
                return result.strip()
        except Exception:
            pass
        return text  # fallback al original

    def _cache_key(self, text: str, account: str, group: str) -> str:
        h = hashlib.sha256(text.encode()).hexdigest()[:12]
        return f"{h}:{account}:{group}"
```

**2. Extender `GeminiCommenter`** con método `generate_text(prompt, max_tokens)` que ya debería existir internamente.

**3. Integrar en `FacebookPoster.publish_to_all_groups`** ([facebook_poster.py:1065](../facebook_auto_poster/facebook_poster.py#L1065)):
```python
# Reemplazar:
if self.config.get("text_variation_mode", False):
    text = _vary_text(text, self.account.name)

# Por:
variator = TextVariator(self._gemini) if self._gemini else None
# ...
for idx, group_id in enumerate(groups):
    variated_text = variator.variate(text, self.account.name, group_id) if variator else text
    success = self.publish(group_id, variated_text, image_path=image_path)
```

**4. Mantener zero-width chars como segunda capa** (defensa en profundidad, no reemplazo). Aplicar el spintax actual *después* del parafraseo.

**5. Cache persistente** (opcional): guardar en tabla `text_variations` en SQLite para no regenerar entre arranques.

### Configuración
```python
CONFIG = {
    ...
    "text_variation_mode": "gemini",  # "gemini" | "zero_width" | "off"
    "text_variation_cache": True,
}
```

### Costo de Gemini
- `gemini-2.5-flash` con ~300 tokens de output: ~$0.00015 por variación.
- 3 cuentas × 5 grupos × 3 posts/día = 45 variaciones/día = $0.007/día. Despreciable.

### Criterio de aceptación
- [ ] Dos publicaciones del mismo texto en dos grupos tienen texto diferente (verificar con diff manual).
- [ ] Los números de teléfono, precios y URLs se mantienen intactos.
- [ ] Si Gemini falla (timeout, quota), el sistema publica el texto original sin romperse.
- [ ] Logs muestran el texto original y el variado para auditoría.

### Riesgos
- **Gemini pierde intención:** baja probabilidad con Flash 2.5, pero revisar muestras semanalmente.
- **Parafraseo introduce errores ortográficos:** el prompt debe especificar "español de México profesional".
- **Cache demasiado agresivo:** si el mismo texto + cuenta + grupo se publica semanas después, reusa parafraseo viejo. Añadir TTL de 7 días.

---

## 2.3 — Pool de workers con límite de concurrencia

### Problema
[api_server.py:361](../facebook_auto_poster/api_server.py#L361) lanza `threading.Thread(...).start()` sin límite. 10 requests simultáneos → 10 Chromes → OOM + señal muy fuerte a Facebook (10 cuentas activas al mismo segundo desde el mismo host).

Además, `sync_playwright` + threading es oficialmente **no thread-safe** — funciona por accidente.

### Solución técnica

**1. Cola persistente + pool controlado:**

Usar la tabla `jobs` en SQLite como cola (ya existe con `status='pending'`). Un proceso consumidor con `ThreadPoolExecutor(max_workers=2)` toma jobs.

**2. Refactor de `api_server.py`:**
```python
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fb-worker")
_running_accounts: set[str] = set()
_lock = Lock()

def _enqueue_job(job_id, accounts, text, image_path, callback_url):
    """Envía el job al pool. El pool serializa por cuenta para evitar colisiones."""
    _executor.submit(_run_job, job_id, accounts, text, image_path, callback_url)
```

**3. Serializar por cuenta:**
Una misma cuenta NO puede tener dos jobs corriendo a la vez (browser profile lockeado). Lock por cuenta:

```python
_account_locks: dict[str, Lock] = {}
_locks_guard = Lock()

def _get_account_lock(name: str) -> Lock:
    with _locks_guard:
        if name not in _account_locks:
            _account_locks[name] = Lock()
        return _account_locks[name]

def _run_job(job_id, accounts, text, image_path, callback_url):
    for account in accounts:
        with _get_account_lock(account.name):
            # procesar esta cuenta
```

**4. Rate limit por cuenta** (nuevo):
Una cuenta no debe publicar más de `max_posts_per_hour` (ej: 3). Chequear en SQLite antes de aceptar job:

```python
def _account_recent_post_count(name: str, window_minutes: int = 60) -> int:
    """Count successful publications in the last N minutes."""
    # Query job_results via JOIN con jobs.finished_at
```

Si excede → encolar para después, no rechazar inmediatamente.

**5. Agregar visibilidad:**
```python
@app.get("/admin/api/queue")
@admin_required
def admin_queue_status():
    return jsonify({
        "active_workers": len(_running_accounts),
        "pending_jobs": job_store.count_pending(),
        "accounts_in_progress": list(_running_accounts),
    })
```

### Configuración
```python
CONFIG = {
    ...
    "max_concurrent_workers": 2,
    "max_posts_per_account_per_hour": 3,
    "max_posts_per_account_per_day": 15,
}
```

### Criterio de aceptación
- [ ] Enviar 10 POST /post simultáneos → solo 2 Chromes corriendo en paralelo, el resto en cola.
- [ ] Intentar publicar 4 veces a la misma cuenta en 1 hora → la 4a se encola para la siguiente hora.
- [ ] `GET /admin/api/queue` muestra estado en tiempo real.
- [ ] Si el servidor reinicia con jobs `running`, se marcan como `failed` o se reencolan (decidir política).

### Riesgos
- **Deadlock entre locks:** solo se toma un lock por cuenta por hilo, no hay anidamiento → sin riesgo.
- **Cola creciente:** si se acumulan cientos de jobs pendientes, alertar. Configurar `max_queue_depth`.

---

## 2.4 — Servidor de producción con waitress

### Problema
[main.py:93](../facebook_auto_poster/main.py#L93): `app.run(host="0.0.0.0", port=port)` — servidor de desarrollo Flask. No thread-safe bajo carga, no maneja keep-alive correctamente, sin graceful shutdown.

### Solución técnica

**1. Añadir dependencia:**
```
waitress==3.0.0
```

**2. Reemplazar en `main.py`:**
```python
from waitress import serve

def main() -> None:
    # ... init como antes
    port = CONFIG.get("api_port", 5000)
    main_logger.info("Sirviendo con waitress en 0.0.0.0:%d", port)
    serve(app, host="0.0.0.0", port=port, threads=8, ident="FBAutoPoster/1.0")
```

`threads=8` es para requests HTTP de OpenClaw/admin — no tiene nada que ver con los workers de browser.

**3. Graceful shutdown:**
Registrar handler de SIGTERM para:
- Dejar de aceptar jobs nuevos.
- Marcar jobs `running` como `interrupted` en DB.
- Cerrar browsers abiertos.
- Detener scheduler_runner.

```python
import signal

def _shutdown_handler(signum, frame):
    main_logger.info("SIGTERM recibido — iniciando shutdown graceful")
    scheduler_runner.stop()
    _executor.shutdown(wait=True, cancel_futures=False)
    sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown_handler)
```

### Criterio de aceptación
- [ ] `python main.py` arranca con waitress (log lo confirma).
- [ ] Flask debug banner ya no aparece.
- [ ] Un `Ctrl+C` no deja Chromes zombies.
- [ ] Apache Bench `ab -n 100 -c 10 http://localhost:5000/accounts` → 100% éxito sin degradación.

### Riesgos
- Ninguno. Waitress es drop-in para WSGI.

---

## 2.5 — Pin de dependencias + auditoría

### Problema
[requirements.txt](../facebook_auto_poster/requirements.txt) usa `>=` en todo. Un `pip install` mañana puede traer patchright 2.0 con breaking changes.

### Solución técnica

**1. Migrar a `uv` o `pip-tools`:**

Con `uv` (recomendado, más rápido):
```bash
uv pip compile requirements.in -o requirements.txt
uv pip sync requirements.txt
```

`requirements.in` (editable, con versiones flexibles):
```
patchright~=1.58
emunium~=3.0
python-dotenv~=1.0
flask~=3.0
waitress~=3.0
requests~=2.31
google-genai~=0.3
cryptography~=42.0
```

`requirements.txt` (generado, pin exacto con hashes):
```
patchright==1.58.3 \
    --hash=sha256:abc...
...
```

**2. Auditoría de dependencias transitivas:**
```bash
uv pip audit    # o: pip-audit
```

Revisar vulnerabilidades conocidas. Priorizar actualización si hay CVE.

**3. CI check** (cuando haya CI): verificar que `uv pip sync --dry-run` no muestra diferencias entre `requirements.txt` y entorno.

### Evaluación de Emunium
Emunium 3.0 tiene mantenimiento limitado. Evaluar alternativas:

| Opción | Pros | Contras |
|--------|------|---------|
| **Emunium** (actual) | Ya integrado, mouse OS-level | Mantenimiento limitado, bug en focus |
| **humancursor** | Modern, Bézier nativo | Solo Selenium/Playwright, no OS-level |
| **PyAutoGUI + Bézier propio** | Control total, estable | Reescribir abstracción |
| **Playwright `mouse.move` steps** | Zero dependencies, integrado | Movimientos geométricos simples |

**Recomendación:** mantener Emunium en esta fase, pero abrir issue para evaluar migración a **humancursor** o una solución custom basada en Bézier en Fase 3. Probar en staging con ambas y comparar detección.

### Criterio de aceptación
- [ ] `requirements.txt` con versiones exactas y hashes.
- [ ] `uv pip audit` devuelve 0 vulnerabilidades críticas.
- [ ] README/CLAUDE.md actualizado con `uv pip sync` como comando de instalación.

### Riesgos
- Algunos paquetes pueden no estar en PyPI con hashes confiables → fallback a pip normal con `==`.

---

## 2.6 — Rate limiter persistente

### Problema
[api_server.py:86-101](../facebook_auto_poster/api_server.py#L86-L101): rate limiter en memoria. Restart del servidor → contador a cero → OpenClaw puede abusar durante ventanas de reinicio.

### Solución técnica

**1. Tabla SQLite:**
```sql
CREATE TABLE IF NOT EXISTS rate_limit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ip         TEXT NOT NULL,
    endpoint   TEXT NOT NULL,
    ts         REAL NOT NULL
);
CREATE INDEX idx_ratelimit_lookup ON rate_limit_events(ip, endpoint, ts);
```

**2. Función:**
```python
def is_rate_limited(ip: str, endpoint: str, limit: int, window_s: int) -> bool:
    now = time.time()
    cutoff = now - window_s
    with _lock, _connect() as conn:
        # Purgar eventos viejos (opcional, job separado)
        conn.execute("DELETE FROM rate_limit_events WHERE ts < ?", (cutoff,))
        count = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_events WHERE ip=? AND endpoint=? AND ts >= ?",
            (ip, endpoint, cutoff)
        ).fetchone()[0]
        if count >= limit:
            return True
        conn.execute(
            "INSERT INTO rate_limit_events (ip, endpoint, ts) VALUES (?, ?, ?)",
            (ip, endpoint, now)
        )
        return False
```

**3. Job de limpieza diario:**
```python
def purge_old_rate_limit_events(days: int = 7):
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM rate_limit_events WHERE ts < ?",
                     (time.time() - days * 86400,))
```

### Criterio de aceptación
- [ ] Reiniciar servidor bajo carga → los contadores se mantienen.
- [ ] Query `SELECT COUNT(*) FROM rate_limit_events` no crece indefinidamente (purga funciona).

### Riesgos
- **Lock contention:** si OpenClaw hace 100 req/s el INSERT en SQLite bajo lock global puede ser cuello. Mitigación: quitar el `_lock` redundante (ver Fase 3) o usar Redis.

---

## 2.7 — Desactivación automática post-ban

### Problema
[facebook_poster.py:604-612](../facebook_auto_poster/facebook_poster.py#L604-L612): al detectar soft-ban, solo loguea y toma screenshot. La cuenta sigue `is_active=1`, el scheduler le sigue asignando jobs, cada job falla, gastas proxy y tiempo.

### Solución técnica

**1. Añadir tabla de bans:**
```sql
CREATE TABLE IF NOT EXISTS account_bans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name    TEXT NOT NULL,
    detected_at     TEXT NOT NULL,
    context         TEXT,
    screenshot_path TEXT,
    reviewed        INTEGER NOT NULL DEFAULT 0
);
```

**2. En `_handle_banned`:**
```python
def _handle_banned(self, context: str) -> None:
    self.logger.critical(...)
    screenshot_path = f"banned_{self.account.name}_{int(time.time())}.png"
    self._screenshot(screenshot_path)
    # Persistir ban
    job_store.record_ban(
        account_name=self.account.name,
        context=context,
        screenshot_path=screenshot_path,
    )
    # Desactivar cuenta
    job_store.set_account_ban_cooldown(self.account.name, hours=48)
```

**3. Nuevo campo en `accounts`:**
```sql
ALTER TABLE accounts ADD COLUMN ban_cooldown_until TEXT;
```

**4. En `load_accounts()`:**
```python
now = datetime.now().isoformat()
# Filtrar cuentas en cooldown
if r.get("ban_cooldown_until") and r["ban_cooldown_until"] > now:
    continue
```

**5. Alertas:**
- Webhook dedicado a OpenClaw: `POST callback_url` con payload `{"event": "account_banned", "account": "maria"}`.
- Opcional: notificación email o Telegram vía `monitoreo.py`.

**6. Panel admin:**
Listar bans en el dashboard con botón "Revisar manualmente" que:
- Abre browser interactivo a esa cuenta.
- Al cerrar, marca `reviewed=1` y resetea `ban_cooldown_until`.

### Criterio de aceptación
- [ ] Cuando se detecta ban, la cuenta queda excluida automáticamente por 48h.
- [ ] OpenClaw recibe callback con evento `account_banned`.
- [ ] Panel admin muestra cuentas en cooldown con tiempo restante.

### Riesgos
- **Falso positivo:** un popup temporal de FB puede ser confundido con ban. Mitigación: requerir 2 detecciones en 10 minutos antes de activar cooldown.

---

## 2.8 — Healthcheck endpoint

### Problema
OpenClaw no tiene forma de saber si el servidor está listo o degradado (proxies caídos, cuentas baneadas, queue saturada).

### Solución técnica

**Endpoint `GET /health`** (sin auth, pero sin info sensible):
```python
@app.get("/health")
def health():
    try:
        with job_store._connect() as conn:
            conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False

    active_accounts = len([a for a in job_store.list_accounts_full()
                            if not a.get("ban_cooldown_until")])
    pending = job_store.count_pending_jobs()

    status = "ok" if db_ok and active_accounts > 0 else "degraded"
    return jsonify({
        "status": status,
        "db": db_ok,
        "active_accounts": active_accounts,
        "pending_jobs": pending,
        "uptime_s": int(time.time() - _START_TIME),
    })
```

Endpoint `GET /health/detailed` (con `X-API-Key`) con más info: cuentas baneadas, jobs por estado, cola de workers, etc.

### Criterio de aceptación
- [ ] `curl http://localhost:5000/health` devuelve 200 con JSON.
- [ ] OpenClaw puede integrarlo en su monitoreo.

### Riesgos
- Ninguno.

---

## 2.9 — Descarga de imágenes no bloqueante en warmup

### Problema
[human_browsing.py:312](../facebook_auto_poster/human_browsing.py#L312): `requests.get(src, timeout=8)` bloquea el thread 8 segundos si la imagen tarda. Durante ese tiempo el browser no interactúa → señal de inactividad sospechosa.

### Solución técnica

**1. Timeout agresivo:**
```python
resp = requests.get(src, timeout=3)  # en vez de 8
```

**2. Si tarda, saltar el comentario:**
El warmup debe continuar (scroll, hover) aunque no se pueda comentar esta publicación específica.

**3. Opcional (mejor):** ejecutar descarga en `ThreadPoolExecutor`:
```python
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

_img_executor = ThreadPoolExecutor(max_workers=2)

def _extract_image_bytes(self, article):
    # ... get src
    future = _img_executor.submit(requests.get, src, timeout=3)
    try:
        resp = future.result(timeout=3.5)
    except FutureTimeout:
        self.logger.debug("[Gemini] Imagen no descargó a tiempo — skip")
        return None, ""
    # ... resto
```

Mientras descarga, el browser puede seguir haciendo micro-interacciones (mover mouse levemente). Aunque en `sync_playwright` con single-threaded eso no es trivial — la mejora real viene en Fase 3 con async.

### Criterio de aceptación
- [ ] Logs muestran que descargar imagen jamás excede 3.5s.
- [ ] Si hay timeout, el warmup continúa con scroll/hover sin comentario Gemini.

### Riesgos
- Reducir tasa de comentarios Gemini (aceptable: el warmup sin comentario sigue aportando).

---

## Orden de implementación sugerido

```
Semana 2:
  Día 1-2:  2.1 (user_data_dir)      ← base de todo lo demás
  Día 3:    2.4 (waitress)           ← rápido, aísla problemas futuros
  Día 3:    2.5 (pin deps)           ← sin cambio funcional
  Día 4-5:  2.3 (pool de workers)    ← depende de 2.4

Semana 3:
  Día 1-2:  2.2 (Gemini variación)
  Día 3:    2.7 (ban automatic)      ← crítico pero simple
  Día 4:    2.6 (rate limit persist)
  Día 4:    2.8 (healthcheck)
  Día 5:    2.9 (img no-block)       ← mejora chica
```

## Métricas de validación de fin de Fase 2

Después de 14 días de operación:
- **Estabilidad:** uptime > 99%, reinicios no-planeados = 0.
- **Cuentas activas:** 100% de cuentas publicando cuando tienen jobs.
- **Diversidad textual:** análisis manual de 20 posts → > 80% tienen variaciones reales.
- **Concurrencia:** 10 req/s sostenido al API sin degradación.
- **Soft-bans:** < 1 por cuenta por semana, todos auto-recuperados en cooldown.
