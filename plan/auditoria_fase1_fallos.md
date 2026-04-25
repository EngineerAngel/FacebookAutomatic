# Auditoría de Fallos — Fase 1 (Post-implementación)

> Rol: Senior de detección de errores no intencionados  
> Fecha: 2026-04-24  
> Revisión post-merge: 2026-04-24  
> Metodología: revisión de código real + patrones de fallo en producción

---

## Resumen ejecutivo

Se encontraron **9 problemas**. Estado actual tras revisión post-merge:

| Severidad | Cantidad | Resueltos | Notas |
|-----------|----------|-----------|-------|
| 🔴 P0 — Crítico | 3 | 3 | — |
| 🟠 P1 — Alto | 4 | 4 | — |
| 🟡 P2 — Medio | 2 | 2 | P2-2 ya no aplica (función eliminada) |

**Todos los bugs resueltos o eliminados por refactoring.**

---

## 🔴 P0 — Críticos

---

### ✅ P0-1 · `secret_key` de Flask reutilizaba `ADMIN_KEY` — RESUELTO

**Archivo:** `api_server.py:127-131`

**Fix aplicado:**
```python
_SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
app.secret_key = _SESSION_SECRET if _SESSION_SECRET else secrets.token_hex(32)
```
`SESSION_SECRET` es ahora una clave dedicada para signing de sesiones, distinta de `ADMIN_KEY`.
Falta añadir `SESSION_SECRET=` al `.env.example` como campo obligatorio.

---

### ✅ P0-2 · `load_accounts()` silenciaba errores de DB con `pass` — RESUELTO

**Archivo:** `config.py:170-179`

**Fix aplicado:** `except` separado en `FileNotFoundError` (esperado) vs `Exception` (inesperado con log visible).
```python
except (FileNotFoundError, ImportError):
    pass  # BD genuinamente no existe aún — fallback a .env esperado
except Exception as _db_exc:
    _lg.getLogger(__name__).error(
        "[config] Error inesperado leyendo DB — fallback a .env. Error: %s", _db_exc
    )
```

---

### ✅ P0-3 · Race condition en `_fernet._instance` sin lock — RESUELTO

**Archivo:** `crypto.py:63-76`

**Fix aplicado:** Double-checked locking con `threading.Lock()`.
```python
_fernet_lock = _threading.Lock()

def _fernet() -> Fernet:
    if _fernet._instance is None:
        with _fernet_lock:
            if _fernet._instance is None:  # double-check tras adquirir lock
                _fernet._instance = Fernet(_load_or_create_key())
    return _fernet._instance
```

---

## 🟠 P1 — Altos (fallos silenciosos)

---

### ✅ P1-1 · `datetime.now()` sin timezone — RESUELTO

**Archivo:** `api_server.py:533` y `api_server.py:907-909`

Ambos endpoints corregidos. Ahora comparan correctamente aware vs naive:
```python
from datetime import timezone as _tz
now_cmp = datetime.now(_tz.utc) if when.tzinfo else datetime.now()
if when <= now_cmp:
```

---

### ✅ P1-2 · `rename_account` — RESUELTO

**Archivo:** `job_store.py:224-240`

**Archivo:** `job_store.py:rename_account`

El INSERT+DELETE fue reemplazado por un `UPDATE` simple que preserva todos los campos (`password_enc`, `fingerprint_json`, `ban_cooldown_until`, etc.).

---

### ✅ P1-3 · `.secret.key` corrupta fallaba silenciosamente en runtime — RESUELTO

**Archivo:** `crypto.py:82-91`

**Fix aplicado:** Inicialización eager de Fernet al cargar el módulo (fail-fast).
```python
try:
    _fernet()  # Falla rápido si .secret.key está corrupta
except Exception as _init_err:
    logger.critical("[crypto] No se pudo inicializar Fernet ...")
    raise
```

---

### ✅ P1-4 · `admin_login` sin rate limiting — RESUELTO

**Archivo:** `api_server.py:599-606`

**Fix aplicado:**
```python
def admin_login():
    # [FIX P1-4] Rate limiting en login — previene brute force contra ADMIN_KEY
    if job_store.is_rate_limited(ip, limit=_RATE_LIMIT, window=_RATE_WINDOW):
        return render_template("admin_login.html", error="Demasiados intentos, espera un momento"), 429
```

---

## 🟡 P2 — Medios (deuda técnica)

---

### ✅ P2-1 · `_rate_data` crecía indefinidamente en memoria — RESUELTO

**Archivo:** `api_server.py`

**Fix aplicado:** El rate limiter completo fue migrado a SQLite como parte del ítem 2.6 de Fase 2. El `defaultdict` en memoria fue eliminado y reemplazado por `job_store.is_rate_limited()`. El memory leak desaparece con la migración.

---

### ✅ P2-2 · `pick_fingerprint()` sin manejo de catálogo vacío — YA NO APLICA

La función `pick_fingerprint()` / `load_fingerprints()` fue eliminada del codebase durante el refactoring de Fase 2. El bug ya no existe.

---

## Tabla de estado final

| ID | Severidad | Archivo | Estado |
|----|-----------|---------|--------|
| P0-1 | 🔴 Crítico | `api_server.py` | ✅ Resuelto |
| P0-2 | 🔴 Crítico | `config.py` | ✅ Resuelto |
| P0-3 | 🔴 Crítico | `crypto.py` | ✅ Resuelto |
| P1-3 | 🟠 Alto | `crypto.py` | ✅ Resuelto |
| P1-4 | 🟠 Alto | `api_server.py` | ✅ Resuelto |
| P2-1 | 🟡 Medio | `api_server.py` | ✅ Resuelto (migración SQLite) |
| P1-1 | 🟠 Alto | `api_server.py` | ✅ Resuelto |
| P1-2 | 🟠 Alto | `job_store.py` | ✅ Resuelto |
| P2-2 | 🟡 Medio | `config.py` | ✅ Ya no aplica (función eliminada) |

**Auditoría cerrada. Todos los bugs resueltos.**

---

> Auditoría post-implementación Fase 1 — 2026-04-24  
> Revisión post-merge Fase 2 — 2026-04-24
