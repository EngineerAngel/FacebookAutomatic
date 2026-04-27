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

## Checklist para migración

Al migrar a rama nueva, verificar:

- [ ] Ejecutar `UPDATE accounts SET is_active=1 WHERE is_active IS NULL;` en la DB
- [ ] Ejecutar `ALTER TABLE account_proxy_assignment ADD COLUMN last_used_at TEXT;` (si no existe — falla silenciosamente)
- [ ] Instalar `PySocks` en el venv: `pip install "requests[socks]"`
- [ ] Confirmar que `config.py` usa `<=` en `is_account_hour_allowed`
- [ ] Confirmar que `publish.html` tiene la función `pollJobStatus`
- [ ] Confirmar que `api_server.py` tiene el endpoint `GET /admin/api/jobs/<job_id>`
- [ ] Probar con `./setup_phone_proxy.sh --status` que no da error SOCKS

---

## Archivos modificados en esta sesión

| Archivo | Cambio |
|---------|--------|
| `config.py` | Fix horario activo: `<` → `<=` |
| `job_store.py` | last_used_at, get_job, count_accounts_for_node, get_lru_account_for_node, touch_proxy_assignment |
| `proxy_manager.py` | Sistema LRU completo, reescrito |
| `api_server.py` | Endpoint GET /admin/api/jobs/<job_id> |
| `templates/admin.html` | Fix JSON.parse sobre array ya deserializado (3 lugares) |
| `templates/publish.html` | Polling real del resultado del job |
| `setup_phone_proxy.sh` | Reescrito: venv Python, nuevos comandos, detección de protocolo |
| `requirements.txt` | PySocks añadido |
