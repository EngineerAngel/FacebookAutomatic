# Flujo de Login y Publicación — Revisión para Integración con Orquestador

> **Basado en:** código real de `facebook_poster_async.py`
> **Propósito:** entender el flujo actual antes de modificar para Fases B y C
> **Última revisión:** Mayo 2026

---

## 1. Flujo actual (sin orquestador)

```
worker_core.run_job()
    │
    ├─ FacebookPosterAsync.__init__()   ← abre Chromium, carga fingerprint, proxy
    │
    ├─ poster.login()
    │     ├─ Intenta restaurar cookies  (_load_cookies + reload + _is_logged_in)
    │     │     OK  → retorna True
    │     │     FAIL → login normal (email/pass + Enter)
    │     │               OK  → guarda cookies, retorna True
    │     │               FAIL (checkpoint/ban/captcha) → retorna False
    │
    ├─ poster.publish_to_all_groups(text, images)
    │     │
    │     └─ for grupo in grupos[:max_groups_per_session]:
    │           │
    │           ├─ if self._banned → marca restantes False, break
    │           ├─ varía texto (zero_width / gemini / off)
    │           ├─ await publish(grupo, texto, images)
    │           └─ await asyncio.sleep(30-60s)   ← espera fija entre grupos
    │
    └─ poster.close()   ← cierra Chromium completamente
```

---

## 2. Métodos clave — contrato actual

### `login() → bool`
- **Hace:** cookies → email/pass → detecta challenge
- **Retorna:** `True` si sesión activa, `False` si fallo
- **Side effects:** `job_store.record_login()`, `metrics.inc_login()`
- **NO modifica:** el state del orquestador (no sabe que existe)

### `_is_logged_in() → bool`
- **Hace:** chequea URL (`/login` → False) + busca 4 XPath del nav de FB
- **Tiempo:** hasta 3s por XPath × 4 = hasta 12s en el peor caso
- **Cuándo se llama hoy:** dentro de `login()` al restaurar cookies
- **Para el orquestador:** se llama **una vez** antes de `publish_to_all_groups()` (decisión §3.1)

### `publish_to_all_groups() → dict[group_id, bool]`
- **Hace:** itera grupos, llama `publish()`, duerme entre grupos
- **Detección de ban:** `if self._banned` antes de cada grupo — para si detecta ban
- **NO hace:** verificar login entre grupos, consumo de feed, notificar al orquestador

### `close() → None`
- **Hace:** cierra `context` y `_pw` (Playwright) completamente
- **Bug conocido (FA-1):** `lock_file.unlink()` nunca se ejecuta si `context.close()` falla — el `.lock` queda huérfano. Fix en Fase C: mover `unlink()` a `finally` independiente.
- **Para sesiones persistentes (Fase C):** necesita `close(really_close=False)` que sólo limpia loggers y handlers sin cerrar el Chromium

---

## 3. Puntos de integración con el orquestador

### 3.1 Verificación de login antes de publicar (decisión tomada)

**Dónde:** en `BrowserSession.publish_job()`, NO dentro del poster.

```python
# orchestrator_async.py — BrowserSession.publish_job()
async def publish_job(self, job: dict) -> dict:
    async with self._job_lock:
        # Verificar sesión una sola vez al inicio
        if not await self.poster._is_logged_in():
            logger.warning("Sesión expirada para %s — re-login", self.account.name)
            self.state = SessionState.LOGGING_IN
            if not await self.poster.login():
                self.state = SessionState.RESTRICTED
                return {"status": "failed", "reason": "login_failed"}
        self.state = SessionState.PUBLISHING
        results = await self.poster.publish_to_all_groups(job["text"], job.get("image_paths"))
        self.state = SessionState.IDLE
        return {"status": "done", "results": results}
```

**Por qué una sola vez:** verificar antes de cada grupo agrega hasta 12s por grupo (4 XPath × 3s). Con 5 grupos = 60s extra. No justifica el beneficio — si la sesión expira entre grupos, el `publish()` falla y el error se captura en `job_results`.

### 3.2 Detención al detectar RESTRICTED durante publicación

`publish_to_all_groups()` ya tiene este patrón:
```python
if self._banned:
    # marca restantes False y hace break
```

El orquestador extiende este patrón para el health score:

```python
# En BrowserSession.publish_job() — loop de grupos
for grupo in grupos:
    if await self.health.is_restricted(self.account.name):
        logger.warning("Cuenta %s RESTRICTED — deteniendo en grupo %s", ...)
        # Marca grupos restantes como skipped en job_results
        break
    await self.poster.publish(grupo, ...)
```

Esto se implementa en **Fase B**, cuando el orquestador controla el loop de grupos.
Por ahora (Fase D), `publish_to_all_groups()` corre completo — la detección de RESTRICTED aplica entre jobs, no entre grupos.

### 3.3 Consumo de feed entre grupos (Fase B)

Hoy `publish_to_all_groups()` duerme 30-60s entre grupos:
```python
await asyncio.sleep(delay)  # delay = random(30, 60)
```

En Fase B, esto se reemplaza por consumo de feed:
```python
await self.consume_feed_async(
    min_s=CONFIG["consume_between_groups_min"],  # 120s
    max_s=CONFIG["consume_between_groups_max"],  # 300s
)
```

**Flag de activación:** `CONSUME_BETWEEN_GROUPS=1` (OFF por defecto).
**Cuándo se reemplaza el sleep:** siempre que la flag esté ON — sin aleatoriedad, sin probabilidad. Cada grupo publica → consume feed → siguiente grupo. Si OFF, comportamiento actual sin cambios.

### 3.4 Sesión persistente — `close(really_close)` (Fase C)

Hoy `close()` cierra todo el Chromium. Para el pool de sesiones del orquestador, el Chromium debe sobrevivir entre jobs.

```python
# Modificación en Fase C
async def close(self, really_close: bool = True) -> None:
    unbind_account()
    if really_close:
        # comportamiento actual: cierra context + playwright
        if self.context:
            await self.context.close()
        if self._pw:
            await self._pw.stop()
    else:
        # sesión persistente: sólo limpia handlers de logging
        # el Chromium sigue vivo
        pass
```

---

## 4. Lo que NO cambia en el poster

| Método | Por qué no se toca |
|--------|--------------------|
| `login()` | Funciona. El orquestador lo llama igual que hoy |
| `_is_logged_in()` | Funciona. Se reutiliza desde el orquestador |
| `publish()` | Núcleo de publicación — no se modifica en ninguna fase |
| `_detect_challenge()` | Detección de ban/checkpoint — ya funciona |
| `_handle_banned()` | Manejo de ban — ya funciona, dispara `account_bans` en DB |

---

## 5. Orden de modificaciones al poster por fase

| Fase | Cambio | Riesgo |
|------|--------|--------|
| A | Ninguno — el poster no sabe del health score | — |
| B | `consume_feed_async`, `consume_feed_cold_async`, `_check_for_checkpoint_async` | MEDIO |
| B | Reemplazar `asyncio.sleep` entre grupos por `consume_feed_async` (si flag ON) | MEDIO |
| C | `close(really_close: bool = True)` | ALTO |
| C | `verify_session_async()` — wrapper de `_is_logged_in` con re-login | MEDIO |
| D+ | Ninguno — el orquestador llama métodos existentes sin modificarlos | — |

**Regla:** `facebook_poster_async.py` sólo se toca en Fases B y C. Las demás fases lo usan como está.
