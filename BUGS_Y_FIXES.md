# Bugs encontrados y fixes aplicados

Documento para migración. Cada bug incluye: dónde está, qué fallaba, qué se cambió.

---

## BUG 1 — Cuentas no aparecen en el panel admin

**Archivo:** `templates/admin.html` (líneas 434, 525, 898)

**Causa:** El endpoint `/admin/api/accounts` ya devuelve `groups` como array Python/JSON. El frontend hacía `JSON.parse(acc.groups)` sobre ese array, lo que causaba un error de sintaxis en JS y rompía el `forEach` completo — resultado: tabla vacía sin mensaje de error.

**Fix:**
```js
// ANTES — falla si groups ya es un array
const groups = JSON.parse(acc.groups || '[]');

// DESPUÉS — maneja ambos casos
const groups = Array.isArray(acc.groups) ? acc.groups : JSON.parse(acc.groups || '[]');
```
Se aplicó en los 3 lugares donde aparecía el mismo patrón.

---

## BUG 2 — El panel dice "Publicado exitosamente" aunque falle

**Archivo:** `templates/publish.html`

**Causa:** El servidor responde `202 ACCEPTED` (el job fue recibido, no completado). El frontend interpretaba ese 202 como éxito y mostraba el mensaje verde inmediatamente, sin esperar el resultado real. Si el job fallaba (horario, proxy caído, etc.) el usuario nunca se enteraba.

**Fix:** Agregar polling al nuevo endpoint `/admin/api/jobs/<job_id>` cada 3 segundos hasta recibir `done` o `failed`. Timeout máximo 5 minutos.

```js
async function pollJobStatus(jobId) {
  // espera hasta que status sea 'done' o 'failed'
  // muestra ✅ éxito, ⚠️ éxito parcial, o ❌ con el mensaje de error real
}
```

Las publicaciones **programadas** no hacen polling (se aceptan y listo).

---

## BUG 3 — No existía endpoint para consultar un job por ID

**Archivos:** `api_server.py`, `job_store.py`

**Causa:** Solo existía `GET /admin/api/jobs` (lista general). El polling del fix anterior necesita consultar un job específico.

**Fix — api_server.py:**
```python
@app.get("/admin/api/jobs/<job_id>")
@admin_required
def admin_job_status(job_id: str):
    job = job_store.get_job(job_id)
    if not job:
        return jsonify({"error": "Job no encontrado"}), 404
    return jsonify(job)
```

**Fix — job_store.py:** Se agregó la función `get_job(job_id)` que retorna el job con sus resultados (groups_ok, groups_fail, errors).

---

## BUG 4 — Cuentas bloqueadas fuera del horario a las 23:xx

**Archivo:** `facebook_auto_poster/config.py` línea 139

**Causa:** La comparación usaba `<` en lugar de `<=`. Con `active_hours=(7, 23)`, a las 23:14 la condición `23 < 23` es `False` → cuenta bloqueada aunque esté dentro del horario configurado.

**Fix:**
```python
# ANTES — excluye la hora 23 completa
return start <= local_hour < end

# DESPUÉS — incluye todo el rango hasta las 23:59
return start <= local_hour <= end
```

---

## BUG 5 — `--status` del script de proxies falla con "Missing dependencies for SOCKS support"

**Archivo:** `setup_phone_proxy.sh`

**Causa:** El script usaba `python3` del sistema (`/usr/bin/python3`), que no tiene `PySocks`. La librería `requests` necesita `PySocks` para proxies SOCKS5. El venv del proyecto sí lo tiene instalado.

**Fix:** Auto-detectar el Python del venv al inicio del script:
```bash
_pick_python() {
    local candidates=(
        "$SCRIPT_DIR/../.venv/bin/python3"
        "$HOME/Proyectos/.venv/bin/python3"
    )
    for p in "${candidates[@]}"; do
        [ -x "$p" ] && echo "$p" && return
    done
    echo "python3"
}
PYTHON="$(_pick_python)"
```
Todos los `python3` del script se reemplazaron por `$PYTHON`.

**Fix adicional — requirements.txt:**
```
PySocks~=1.7    # soporte SOCKS5 para requests
```

---

## BUG 6 — is_active=NULL en cuentas antiguas (no aparecían en list_accounts_full)

**Archivo:** `job_store.py`

**Causa:** La columna `is_active` se añadió con `ALTER TABLE ... DEFAULT 1`, pero SQLite no retroactivamente actualiza filas existentes en algunos casos. Las cuentas creadas antes de esa migración tenían `is_active=NULL`. `list_accounts_full()` filtra con `WHERE is_active=1`, por lo que no las retornaba.

**Fix aplicado en DB (una sola vez):**
```sql
UPDATE accounts SET is_active=1 WHERE is_active IS NULL;
```

**Para la migración:** Ejecutar esto después de migrar la DB si las cuentas no aparecen en el panel.

---

## MEJORAS IMPLEMENTADAS (no eran bugs, pero son importantes)

### Sistema de proxies SIM — asignación dinámica LRU

**Archivos:** `proxy_manager.py`, `job_store.py`

**Qué hace:** Cuando una cuenta necesita publicar y no tiene proxy asignado, el sistema lo asigna automáticamente. Si todos los nodos están llenos, expulsa la cuenta que lleva más tiempo sin publicar (LRU) y le da el proxy a la que lo necesita ahora.

**Cambios en job_store.py:**
- Nueva columna `last_used_at` en `account_proxy_assignment`
- `touch_proxy_assignment(account)` — actualiza last_used_at al publicar
- `count_accounts_for_node(node_id)` — para verificar capacidad
- `get_lru_account_for_node(node_id)` — candidata a expulsión

**Cambios en proxy_manager.py:**
- `MAX_ACCOUNTS_PER_NODE = 10` — capacidad por teléfono
- `_assign_to_free_slot()` — busca nodo con espacio libre
- `_evict_lru_and_assign()` — expulsa LRU y asigna
- `resolve_proxy()` — ahora asigna automáticamente si la cuenta no tiene proxy

### Script setup_phone_proxy.sh — reescrito completamente

Nuevos comandos:
- `--edit NODE_ID` — editar servidor/etiqueta/notas de un nodo existente
- `--remove NODE_ID` — eliminar nodo con confirmación
- `--info NODE_ID` — ver todos los detalles
- `--fix NODE_ID` — re-detectar IP/puerto si el teléfono cambió de IP
- `--assign NODE_ID CUENTA` — asignar proxy manualmente
- `--unassign CUENTA` — quitar proxy de cuenta
- `--test URL [N]` — probar con N reintentos

Mejoras al escaneo automático:
- Detecta protocolo automáticamente: SOCKS5, HTTP, SOCKS4 (antes solo SOCKS5)
- Escanea 9 puertos en lugar de 5
- Diagnóstico detallado cuando falla (por protocolo, por app, por WiFi/SIM)

---

## BUG 7 — `groups` llega como string JSON al frontend (~500 grupos falsos)

**Archivo:** `api_server.py` — `admin_list_accounts()`

**Causa:** El campo `groups` se guarda en SQLite como JSON string (`'["111","222"]'`). El endpoint lo parseaba internamente pero **no reasignaba el resultado** al dict antes de serializarlo. El frontend recibía el string crudo.

```python
# ANTES — groups_list se parsea pero nunca se devuelve
groups_list = json.loads(r.get("groups") or "[]")
r["has_groups"] = len(groups_list) > 0
# r["groups"] sigue siendo el string original

# DESPUÉS
groups_list = json.loads(r.get("groups") or "[]")
r["groups"] = groups_list   # ← asignar el array real
r["has_groups"] = len(groups_list) > 0
```

**Síntomas:**
- `acc.groups.length` devolvía ~500 (longitud del string, no cantidad de grupos)
- La sección de grupos del paso 3 en `publish.html` no renderizaba (`.forEach` falla en strings)
- El panel mostraba "500 grupo(s)" para cuentas con 3-5 grupos reales

**Patrón a vigilar en migración:** Siempre que un campo de BD sea JSON string, reasignarlo como array antes de `jsonify()`.

---

## BUG 8 — `selectTemplate()` falla en Firefox con `event.currentTarget`

**Archivo:** `templates/publish.html`

**Causa:** `event.currentTarget` es un implicit global (`window.event`) que Firefox no soporta. En Chrome funciona por accidente, pero es undefined en Firefox y Safari.

```js
// ANTES — falla en Firefox/Safari
event.currentTarget.classList.add('selected');

// DESPUÉS — busca el card correcto por índice en state.allTemplates
document.querySelectorAll('.template-card').forEach((card, idx) => {
  const cardData = state.allTemplates[idx];
  if (cardData && cardData.id === tplId) {
    card.classList.add('selected');
  } else {
    card.classList.remove('selected');
  }
});
```

---

## BUG 9 — `publish()` envía `scheduled_for=null` al servidor

**Archivo:** `templates/publish.html`

**Causa:** No había validación de `state.publishDatetime` antes de armar el `FormData`. Si el usuario llegaba al paso Confirmar sin poner fecha, enviaba `scheduled_for=null` → el servidor fallaba con `ValueError` al parsear.

**Fix:** Validar antes de deshabilitar el botón:
```js
if (state.publishWhen === 'scheduled') {
  if (!state.publishDatetime || state.publishDatetime.trim() === '') {
    showStatus('confirm-status', 'Selecciona fecha y hora', 'error');
    return;
  }
  const scheduled = new Date(state.publishDatetime);
  if (isNaN(scheduled.getTime()) || scheduled <= new Date()) {
    showStatus('confirm-status', 'La fecha debe ser en el futuro', 'error');
    return;
  }
}
```

---

## BUG 10 — `loadTemplates()` no detecta errores HTTP

**Archivo:** `templates/publish.html`

**Causa:** `fetch()` no lanza excepción en errores HTTP (401, 500). El código hacía `res.json()` directamente — si el servidor devolvía un objeto `{error: "..."}` en vez de array, el `forEach` posterior fallaba silenciosamente.

```js
// ANTES — ignora res.ok
const res = await fetch('/admin/api/templates');
const templates = await res.json();  // puede ser {error: "..."} si 401/500
state.allTemplates = templates;      // TypeError en forEach

// DESPUÉS
if (!res.ok) {
  const err = await res.json().catch(() => ({}));
  throw new Error(err.error || `HTTP ${res.status}`);
}
const templates = await res.json();
if (!Array.isArray(templates)) throw new Error('Respuesta inválida');
```

**Mismo patrón aplicado a `loadAccountsData()`.**

---

## BUG 11 — XSS en `showTemplatePreview()` via `innerHTML`

**Archivo:** `templates/publish.html`

**Causa:** La función construía un string HTML con datos del servidor y lo asignaba a `body.innerHTML`. Si `image_path` contenía `javascript:alert('xss')`, `escapeHtml()` lo dejaba pasar como atributo `src`.

**Fix:** Construir el DOM con `createElement` + `textContent`, y validar que `image_path` empiece con `/`:

```js
// ANTES — peligroso
body.innerHTML = `<img src="${escapeHtml(tpl.image_path)}">`;

// DESPUÉS — seguro
if (tpl.image_path && tpl.image_path.startsWith('/')) {
  const img = document.createElement('img');
  img.src = tpl.image_path;  // solo rutas locales
  body.appendChild(img);
}
```

---

## BUG 12 — Endpoints de plantillas no validan formato de `template_id`

**Archivo:** `api_server.py` — endpoints GET/PUT/DELETE `/admin/api/templates/<template_id>`

**Causa:** Sin validación, un `template_id` con caracteres arbitrarios llegaba directamente a `job_store`. Aunque las queries son parametrizadas (no hay SQL injection), es mejor rechazar IDs malformados en el límite.

**Fix:** Validar con regex antes de consultar la BD:
```python
_TEMPLATE_ID_PATTERN = re.compile(r'^[a-f0-9]{12}$')

def _validate_template_id(template_id: str) -> bool:
    return bool(_TEMPLATE_ID_PATTERN.match(template_id))

# En cada endpoint:
if not _validate_template_id(template_id):
    return jsonify({"error": "ID de plantilla inválido"}), 400
```

---

## BUG 13 — Sin límites de tamaño en campos de plantilla

**Archivo:** `api_server.py`

**Causa:** Solo había validación de mínimo (10 chars en texto), pero no de máximo. Un texto de 10 MB pasaría la validación y se guardaría en la BD.

**Fix:** Definir constantes y validar en create y update:
```python
MAX_TEMPLATE_TEXT_CHARS = 50000   # 50 KB
MAX_TEMPLATE_NAME_CHARS = 100
MAX_TEMPLATE_URL_CHARS  = 2048
MIN_TEMPLATE_TEXT_CHARS = 10
```

---

## BUG 14 — Race condition en `assign_proxy_to_account()`

**Archivo:** `proxy_manager.py`

**Causa:** La función leía la lista de nodos, calculaba el mejor, y luego asignaba — sin lock. Dos threads ejecutando simultáneamente podían asignar dos cuentas al mismo nodo sin detectar el solapamiento.

**Fix:** Envolver con `_assign_lock = threading.Lock()`:
```python
_assign_lock = threading.Lock()

def assign_proxy_to_account(...):
    with _assign_lock:
        nodes = job_store.list_proxy_nodes()
        # ... calcular + asignar en transacción atómica
```

---

## BUG 15 — `_check_node()` ignora errores de JSON en respuesta del proxy

**Archivo:** `proxy_manager.py`

**Causa:** Si el proxy devolvía una respuesta HTTP 200 pero con body HTML (página de error del ISP), `resp.json()` lanzaba excepción que caía al `except Exception` genérico — el nodo se marcaba offline cuando en realidad el proxy sí conectaba.

**Fix:** Separar los `except` por tipo:
```python
except requests.Timeout:
    return False, ""
except requests.ConnectionError:
    return False, ""
except Exception:
    logger.exception("[ProxyCheck] %s error inesperado", node["id"])
    return False, ""
```
Y validar el JSON por separado con su propio try/except que loguea `warning` en vez de marcar offline.

---

## BUG 16 — `admin_assign_proxy()` no valida que los nodos existan

**Archivo:** `api_server.py`

**Causa:** En la asignación manual de proxy, si `primary_node` no existía en BD, la función `job_store.set_proxy_assignment()` fallaba con FK constraint error sin retornar un mensaje útil al cliente.

**Fix:**
```python
if not job_store.get_proxy_node(primary):
    return jsonify({"error": f"Nodo primario '{primary}' no existe"}), 404
if secondary and not job_store.get_proxy_node(secondary):
    return jsonify({"error": f"Nodo secundario '{secondary}' no existe"}), 404
try:
    job_store.set_proxy_assignment(name, primary, secondary)
except Exception:
    logger.exception("Error asignando proxy a '%s':", name)
    return jsonify({"error": "Error en base de datos"}), 500
```

---

## BUG 17 — `_read_static_url()` falla si el archivo del túnel está vacío

**Archivo:** `main.py`

**Causa:** `_read_static_url()` hacía `_URL_FILE.read_text().strip()` sin verificar si el archivo existía o tenía contenido válido. Si el archivo estaba vacío o contenía una ruta no-URL, el startup continuaba con una URL vacía — los webhook callbacks fallaban silenciosamente.

**Fix:**
```python
def _read_static_url() -> str | None:
    if not _URL_FILE.exists():
        return None
    url = _URL_FILE.read_text().strip()
    if not url or not url.startswith(("http://", "https://")):
        logger.error("[Tunnel] URL inválida en %s: '%s'", _URL_FILE, url)
        return None
    return url
```
Igual para `_read_backend()` — validar que el valor sea `"cloudflare"` o `"ngrok"`.

---

## Checklist para migración

Al migrar a rama nueva, verificar:

**Base de datos:**
- [ ] `UPDATE accounts SET is_active=1 WHERE is_active IS NULL;`
- [ ] `ALTER TABLE account_proxy_assignment ADD COLUMN last_used_at TEXT;` (falla silenciosamente si ya existe)

**Dependencias:**
- [ ] `pip install "requests[socks]"` — soporte SOCKS5
- [ ] Confirmar `PySocks` en `requirements.txt`

**Backend — api_server.py:**
- [ ] `admin_list_accounts()`: `r["groups"] = groups_list` está presente (no solo `has_groups`)
- [ ] `admin_list_templates()`, `admin_create_template()`, etc. usan `logger.exception()` no `logger.error()`
- [ ] Constantes `MAX_TEMPLATE_TEXT_CHARS`, `MIN_TEMPLATE_TEXT_CHARS` definidas antes de los endpoints
- [ ] `_validate_template_id()` definida y llamada en GET/PUT/DELETE de templates
- [ ] `admin_assign_proxy()` valida nodos con `get_proxy_node()` antes de `set_proxy_assignment()`
- [ ] Endpoint `GET /admin/api/jobs/<job_id>` existe

**Backend — config.py:**
- [ ] `is_account_hour_allowed` usa `<=` en el extremo superior (no `<`)

**Backend — proxy_manager.py:**
- [ ] `_assign_lock = threading.Lock()` declarado globalmente
- [ ] `assign_proxy_to_account()` usa `with _assign_lock:`
- [ ] `_check_node()` tiene `except requests.Timeout` y `except requests.ConnectionError` separados
- [ ] `resolve_proxy()` implementa cache con `_proxy_cache` dict + `_PROXY_CACHE_TTL_S`

**Backend — main.py:**
- [ ] `_read_static_url()` retorna `str | None` y valida contenido
- [ ] `_read_backend()` valida que sea `"cloudflare"` o `"ngrok"`
- [ ] `_ensure_tunnel_ready()` existe y se usa en `start_tunnel()`

**Frontend — publish.html:**
- [ ] `loadTemplates()` valida `res.ok` y `Array.isArray()`
- [ ] `loadAccountsData()` valida `res.ok` y `Array.isArray()`
- [ ] `selectTemplate()` no usa `event.currentTarget`
- [ ] `showTemplatePreview()` usa `createElement` + `textContent` (no `innerHTML` con datos externos)
- [ ] `publish()` valida `scheduled_for` antes de deshabilitar el botón
- [ ] `publish()` tiene polling con `pollJobStatus()` o verifica `res.ok` antes de resetear

**Tests:**
- [ ] Ejecutar `python3 test_code_verification.py` → debe dar 13/13

---

## Archivos modificados en esta sesión

| Archivo | Cambio |
|---------|--------|
| `config.py` | Fix horario activo: `<` → `<=` |
| `job_store.py` | last_used_at, get_job, count_accounts_for_node, get_lru_account_for_node, touch_proxy_assignment |
| `proxy_manager.py` | LRU, lock en assign, cache en resolve, validaciones en _check_node, _alert_node_down |
| `api_server.py` | groups deserialization, template validation, proxy endpoint validation, job endpoint |
| `templates/admin.html` | Fix JSON.parse sobre array ya deserializado (3 lugares) |
| `templates/publish.html` | Firefox fix, scheduled_for, loadTemplates/loadAccounts error handling, XSS fix |
| `main.py` | _read_static_url, _read_backend, _ensure_tunnel_ready con validación |
| `setup_phone_proxy.sh` | Reescrito: venv Python, nuevos comandos, detección de protocolo |
| `requirements.txt` | PySocks añadido |
