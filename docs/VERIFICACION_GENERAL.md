# Verificacion de Bugs — Generales (Admin, DB, Grupos, Horario)

> Rama: `produccion_temp`
> Proposito: Checklist para verificar si los bugs corregidos aqui tambien estan presentes en la otra rama.

Cada bug incluye: sintoma observable, como verificar si existe en la otra rama, y referencia del fix aplicado.

---

## G1 — Cuentas no aparecen en el panel admin

- **Archivo:** `templates/admin.html`
- **Sintoma:** La tabla de cuentas aparece vacia sin mensaje de error. El endpoint `/admin/api/accounts` devuelve `groups` como array JSON, pero el frontend hacia `JSON.parse(acc.groups)` sobre ese array, causando error de sintaxis en JS que rompe el `forEach`.
- **Verificacion:** Abrir `/admin` con cuentas que tengan grupos. Buscar `JSON.parse(acc.groups)` en el JS del frontend. Si no tiene guard `Array.isArray(acc.groups)`, el bug esta presente.
- **Fix aplicado:**
  ```js
  // ANTES — falla si groups ya es un array
  const groups = JSON.parse(acc.groups || '[]');
  // DESPUES — maneja ambos casos
  const groups = Array.isArray(acc.groups) ? acc.groups : JSON.parse(acc.groups || '[]');
  ```
  Se aplico en 3 lugares donde aparecia el mismo patron.
- **Archivos modificados:** `templates/admin.html`

---

## G2 — Panel dice "Publicado exitosamente" aunque el job falle

- **Archivo:** `templates/publish.html`
- **Sintoma:** El servidor responde `202 ACCEPTED` (el job fue recibido, no completado). El frontend interpreta 202 como exito y muestra mensaje verde inmediatamente. Si el job falla despues, el usuario nunca se entera.
- **Verificacion:** Publicar algo y ver si el frontend muestra "Publicado exitosamente" inmediatamente sin esperar el resultado real. Buscar la funcion `pollJobStatus()` en `publish.html`.
- **Fix aplicado:** Polling al endpoint `/admin/api/jobs/<job_id>` cada 3s hasta recibir `done` o `failed`. Timeout maximo 5 minutos. Las publicaciones programadas no hacen polling.
- **Archivos modificados:** `templates/publish.html`

---

## G3 — No existe endpoint `GET /admin/api/jobs/<job_id>`

- **Archivos:** `api_server.py`, `job_store.py`
- **Sintoma:** No hay forma de consultar el estado de un job individual. Necesario para el polling del fix G2.
- **Verificacion:** Buscar `def admin_job_status(job_id)` en `api_server.py` y `def get_job(job_id)` en `job_store.py`. Hacer `GET /admin/api/jobs/<id>` — si devuelve 404, el endpoint no existe.
- **Fix aplicado:**
  - `api_server.py`: `@app.get("/admin/api/jobs/<job_id>")` con `@admin_required`
  - `job_store.py`: `get_job(job_id)` que retorna el job con resultados (groups_ok, groups_fail, errors)
- **Archivos modificados:** `api_server.py`, `job_store.py`

---

## G4 — Cuentas bloqueadas fuera de horario a las 23:xx

- **Archivo:** `facebook_auto_poster/config.py`
- **Sintoma:** Con `active_hours=(7, 23)`, a las 23:14 la condicion `23 < 23` es `False` → cuenta bloqueada aunque este dentro del horario configurado.
- **Verificacion:** Buscar `is_account_hour_allowed()` en `config.py`. Si la comparacion usa `<` en vez de `<=` para el extremo superior, el bug esta presente.
- **Fix aplicado:**
  ```python
  # ANTES — excluye la hora 23 completa
  return start <= local_hour < end
  # DESPUES — incluye hasta las 23:59
  return start <= local_hour <= end
  ```
- **Archivos modificados:** `config.py`

---

## G5 — `is_active=NULL` en cuentas antiguas

- **Archivo:** `job_store.py`
- **Sintoma:** La columna `is_active` se agrego con `ALTER TABLE ... DEFAULT 1`, pero SQLite no actualiza filas existentes retroactivamente. Cuentas creadas antes de la migracion tienen `is_active=NULL` y `list_accounts_full()` las filtra con `WHERE is_active=1`.
- **Verificacion:** Ejecutar `SELECT name, is_active FROM accounts WHERE is_active IS NULL;` en la BD. Si hay resultados, el bug esta presente.
- **Fix aplicado (en BD, una sola vez):**
  ```sql
  UPDATE accounts SET is_active=1 WHERE is_active IS NULL;
  ```
- **Archivos modificados:** Ninguno (fix manual en BD). Documentar en instrucciones de migracion.

---

## G6 — `groups` llega como string JSON al frontend (~500 "grupos" falsos)

- **Archivo:** `api_server.py` — `admin_list_accounts()`
- **Sintoma:** El campo `groups` se guarda en SQLite como JSON string (`'["111","222"]'`). El endpoint lo parseaba pero **no reasignaba el resultado** al dict. El frontend recibe el string crudo y `acc.groups.length` devuelve ~500 (longitud del string).
- **Verificacion:** Llamar `GET /admin/api/accounts`. Si `groups` es un string en vez de array, el bug esta presente.
- **Fix aplicado:**
  ```python
  # ANTES — groups_list se parsea pero no se devuelve
  groups_list = json.loads(r.get("groups") or "[]")
  r["has_groups"] = len(groups_list) > 0
  # r["groups"] sigue siendo el string original

  # DESPUES
  groups_list = json.loads(r.get("groups") or "[]")
  r["groups"] = groups_list   # asignar el array real
  r["has_groups"] = len(groups_list) > 0
  ```
- **Archivos modificados:** `api_server.py`

---

## G7 — Descubrimiento de grupos falla en cuentas sin grupos

- **Archivo:** `api_server.py` — `_run_discovery()`
- **Sintoma:** `_run_discovery()` llama `load_accounts()` que filtra cuentas sin grupos. Las cuentas sin grupos son justo las que necesitan el descubrimiento — siempre fallan con `"Cuenta 'X' no encontrada"`.
- **Verificacion:** Intentar ejecutar descubrimiento de grupos en una cuenta sin grupos configurados. Si devuelve 404 "no encontrada", el bug esta presente.
- **Fix aplicado:** Leer directo de `job_store.list_accounts_full()` (sin filtro de grupos) y construir `AccountConfig` manualmente.
- **Archivos modificados:** `api_server.py`

---

## G8 — Descubrimiento de grupos navega sin hacer login

- **Archivo:** `group_discoverer.py` — `discover_groups_for_account()`
- **Sintoma:** El codigo crea `FacebookPoster` y llama `poster.page.goto()` sin `poster.login()`. Para cuentas sin cookies, Facebook redirige al login y el script extrae 0 grupos.
- **Verificacion:** Buscar si `discover_groups_for_account()` llama `poster.login()` antes de `poster.page.goto()`. Si no lo hace, el bug esta presente.
- **Fix aplicado:**
  ```python
  # ANTES — sin login
  poster = FacebookPoster(account, config)
  poster.page.goto("https://www.facebook.com/groups/joins/")
  # DESPUES — login explicito
  poster = FacebookPoster(account, config)
  if not poster.login():
      raise RuntimeError("Login fallido — verifica credenciales")
  poster.page.goto("https://www.facebook.com/groups/joins/")
  ```
- **Archivos modificados:** `group_discoverer.py`

---

## G9 — Guards de descubrimiento usan `load_accounts()` en vez de `list_accounts_full()`

- **Archivo:** `api_server.py` — `admin_trigger_discovery()`, `admin_list_discovered_groups()`
- **Sintoma:** Ambos endpoints tienen guard que llama `load_accounts()`, que **filtra cuentas sin grupos**. El endpoint rechaza la peticion con 404 antes de llegar al thread de descubrimiento.
- **Verificacion:** Buscar si los guards usan `load_accounts()` o `job_store.list_accounts_full()`. Si usan `load_accounts()`, el bug esta presente.
- **Fix aplicado:** Reemplazar `load_accounts()` por `job_store.list_accounts_full()` en ambos guards.
- **Archivos modificados:** `api_server.py`

**Regla general:** `load_accounts()` es **exclusivo del pipeline de publicacion** — requiere grupos para funcionar. Todo lo demas (discovery, listado, verificacion) debe usar `job_store.list_accounts_full()`.

---

## G10 — `_read_static_url()` no valida contenido (vacio o no-URL)

- **Archivo:** `main.py`
- **Sintoma:** `_read_static_url()` hace `_URL_FILE.read_text().strip()` sin verificar que el contenido sea una URL valida. Si el archivo esta vacio o contiene texto no-URL, el startup continua con URL vacia y los webhook callbacks fallan silenciosamente.
- **Verificacion:** Buscar si `_read_static_url()` retorna `str | None` y valida que el contenido empiece con `http://` o `https://`.
- **Fix aplicado:**
  - Retornar `None` si el archivo no existe
  - Validar que el contenido no este vacio y empiece con `http://` o `https://`
  - Log `ERROR` si el archivo tiene contenido invalido
- **Archivos modificados:** `main.py`

---

## Nota: `_ensure_tunnel_ready()` centraliza validacion

Si la otra rama tiene el patron disperso de `_read_static_url()` + `_read_backend()` en varios lugares del startup, considerar consolidar en `_ensure_tunnel_ready()`:

```python
def _ensure_tunnel_ready() -> tuple[str | None, str | None]:
    url = _read_static_url()
    backend = _read_backend()
    if url and backend:
        logger.info("[Tunnel] URL estatica: %s (%s)", url, backend)
        return url, backend
    logger.info("[Tunnel] Usando tunnel dinamico")
    return None, None
```
