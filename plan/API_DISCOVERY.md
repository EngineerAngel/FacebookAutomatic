# API Discovery — Referencia técnica Fase 2.10

Documentación técnica de endpoints y flujos para auto-descubrimiento de grupos.

---

## Endpoints

### 1. Trigger Descubrimiento

```
POST /admin/api/accounts/<name>/discover-groups
```

**Autenticación:** Admin required (`@admin_required`)

**Parámetros:**
- `<name>` — Nombre de cuenta (path param)
- Body: (vacío)

**Respuesta exitosa (202 Accepted):**
```json
{
  "run_id": "a1b2c3d4e5f6",
  "status": "running"
}
```

**Respuesta error (404):**
```json
{
  "error": "Cuenta 'maria' no encontrada"
}
```

**Lógica del servidor:**
1. Carga todas las cuentas desde `load_accounts()`
2. Busca coincidencia exacta por nombre
3. Si no existe: retorna 404
4. Si existe:
   - Genera `run_id = uuid.uuid4().hex[:12]` (ej: `a1b2c3d4e5f6`)
   - Inserta en DB: `job_store.create_discovery_run(run_id, name)`
   - Lanza thread daemon: `_run_discovery(run_id, name)`
   - Retorna inmediatamente con 202

**Duración esperada:** El endpoint retorna en <100ms. El descubrimiento real toma 15-30 segundos en el thread.

---

### 2. Polling Estado

```
GET /admin/api/discovery/<run_id>
```

**Autenticación:** Admin required

**Parámetros:**
- `<run_id>` — ID del run (path param, ej: `a1b2c3d4e5f6`)

**Respuesta exitosa (200):**
```json
{
  "id": "a1b2c3d4e5f6",
  "account_name": "maria",
  "status": "running",
  "started_at": "2026-04-25T14:32:10.123456",
  "finished_at": null,
  "groups_found": null,
  "error": null
}
```

O (cuando termina exitosamente):
```json
{
  "id": "a1b2c3d4e5f6",
  "account_name": "maria",
  "status": "done",
  "started_at": "2026-04-25T14:32:10.123456",
  "finished_at": "2026-04-25T14:32:45.654321",
  "groups_found": 14,
  "error": null
}
```

O (si falla):
```json
{
  "id": "a1b2c3d4e5f6",
  "account_name": "maria",
  "status": "failed",
  "started_at": "2026-04-25T14:32:10.123456",
  "finished_at": "2026-04-25T14:32:25.654321",
  "groups_found": 0,
  "error": "Timeout while waiting for element"
}
```

**Respuesta error (404):**
```json
{
  "error": "run_id no encontrado"
}
```

**Estados del run:**
- `"running"` — En progreso. Pollear de nuevo en 3 segundos.
- `"done"` — Completado exitosamente. `groups_found` contiene count.
- `"failed"` — Fallo. Ver campo `error` para detalles (primeros 500 caracteres).

**Campos de timestamp:**
- ISO 8601 format: `2026-04-25T14:32:10.123456`
- `finished_at` es `null` mientras `status = "running"`

---

### 3. Listar Grupos Descubiertos

```
GET /admin/api/accounts/<name>/discovered-groups
```

**Autenticación:** Admin required

**Parámetros:**
- `<name>` — Nombre de cuenta (path param)

**Respuesta exitosa (200):**
```json
{
  "account": "maria",
  "groups": [
    {
      "group_id": "123456789",
      "group_name": "Roomies CDMX",
      "discovered_at": "2026-04-25T14:32:45.123456",
      "added_to_posting": 0,
      "last_seen": "2026-04-25T14:32:45.123456"
    },
    {
      "group_id": "987654321",
      "group_name": "Python Argentina",
      "discovered_at": "2026-04-25T14:32:45.123456",
      "added_to_posting": 1,
      "last_seen": "2026-04-25T14:32:45.123456"
    }
  ],
  "total": 14,
  "pending": 13
}
```

**Respuesta error (404):**
```json
{
  "error": "Cuenta 'maria' no encontrada"
}
```

**Lógica:**
1. Verifica que la cuenta existe (carga desde config)
2. Query `SELECT ... FROM discovered_groups WHERE account_name=? ORDER BY added_to_posting ASC, discovered_at DESC`
3. Retorna lista ordenada: primero pendientes (added_to_posting=0), luego añadidos (1)
4. Estadísticas:
   - `total`: cantidad de filas retornadas
   - `pending`: count WHERE added_to_posting=0

**Campos por grupo:**
- `group_id` — ID de grupo de Facebook (string de dígitos)
- `group_name` — Nombre extraído del DOM (puede tener caracteres no-ASCII)
- `discovered_at` — Timestamp cuando fue descubierto (ISO 8601)
- `added_to_posting` — 0 = pendiente, 1 = aprobado y añadido a lista activa
- `last_seen` — Último timestamp en que fue visto (actualizado si se redescubre)

---

### 4. Añadir Grupo a Publicación

```
POST /admin/api/accounts/<name>/discovered-groups/<group_id>/add
```

**Autenticación:** Admin required

**Parámetros:**
- `<name>` — Nombre de cuenta (path param)
- `<group_id>` — ID del grupo (path param, ej: `123456789`)
- Body: (vacío)

**Respuesta exitosa (200):**
```json
{
  "status": "added",
  "account": "maria",
  "group_id": "123456789"
}
```

**Respuesta error (404):**
```json
{
  "error": "Cuenta 'maria' no encontrada"
}
```

**Lógica:**
1. Carga detalles de cuenta desde `job_store.list_accounts_full()`
2. Si no existe: retorna 404
3. Si existe:
   - Extrae `groups` (JSON list) desde DB
   - Si `group_id` no está en la lista: lo añade
   - Actualiza DB: `job_store.update_account(name, email, groups)`
   - Marca grupo: `job_store.mark_group_added_to_posting(name, group_id)`
   - Loguea a INFO
   - Retorna 200

**Efecto secundario:**
- El grupo aparece en:
  - `accounts[name].groups` (lista de publicación activa)
  - `discovered_groups[account_name, group_id].added_to_posting = 1`

---

## Flujo de cliente (JavaScript)

### Trigger + Polling

```javascript
async function triggerDiscovery(accountName, btn) {
  btn.disabled = true;
  btn.textContent = '⏳';

  const { ok, data } = await api('POST', `/admin/api/accounts/${accountName}/discover-groups`);
  if (!ok) {
    toast(`Error: ${data?.error ?? 'unknown'}`, true);
    btn.disabled = false;
    btn.textContent = '🔍';
    return;
  }

  const runId = data.run_id;
  pollDiscovery(runId, accountName, btn);
}

function pollDiscovery(runId, accountName, btn) {
  const pollInterval = setInterval(async () => {
    const { ok, data } = await api('GET', `/admin/api/discovery/${runId}`);
    
    if (!ok) {
      clearInterval(pollInterval);
      btn.disabled = false;
      btn.textContent = '🔍';
      return;
    }

    const run = data;

    if (run.status === 'done') {
      clearInterval(pollInterval);
      btn.disabled = false;
      btn.textContent = '🔍';
      toast(`✅ ${run.groups_found} grupos encontrados para ${accountName}`);
      loadAccounts();
    } else if (run.status === 'failed') {
      clearInterval(pollInterval);
      btn.disabled = false;
      btn.textContent = '🔍';
      toast(`❌ Error: ${run.error}`, true);
    }
    // Si status === 'running': continúa polleando
  }, 3000); // Poll cada 3 segundos
}
```

**Flujo:**
1. POST endpoint → recibe run_id inmediatamente
2. Inicia setInterval que llama GET cada 3 segundos
3. Lee status del objeto retornado
4. Si "done" o "failed": limpia interval, actualiza UI, muestra toast
5. Si "running": continúa esperando

**Timings típicos:**
- POST (trigger): ~50ms
- Descubrimiento (en thread): ~15-30 segundos
- Total polling (3s * N intentos): ~15-30 segundos
- UX: botón muestra ⏳ durante ese tiempo

---

## Estructura de tablas

### discovery_runs

```sql
CREATE TABLE discovery_runs (
  id            TEXT PRIMARY KEY,
  account_name  TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'running',  -- 'running' | 'done' | 'failed'
  started_at    TEXT NOT NULL,                    -- ISO 8601
  finished_at   TEXT,                             -- NULL si running
  groups_found  INTEGER DEFAULT 0,
  error         TEXT                              -- primeros 500 chars si failed
);

CREATE INDEX idx_discovery_account ON discovery_runs(account_name);
```

**Ciclo de vida:**
1. INSERT: `create_discovery_run(run_id, account_name)` → status='running', finished_at=NULL
2. UPDATE: `finish_discovery_run(run_id, groups_found)` → status='done', finished_at=NOW(), groups_found=N
   O: `fail_discovery_run(run_id, error)` → status='failed', finished_at=NOW(), error=msg

**Retención:** Borrar runs más viejos de 30 días (futuro admin task)

### discovered_groups

```sql
CREATE TABLE discovered_groups (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  account_name     TEXT NOT NULL,
  group_id         TEXT NOT NULL,
  group_name       TEXT NOT NULL,
  discovered_at    TEXT NOT NULL,                 -- ISO 8601
  added_to_posting INTEGER NOT NULL DEFAULT 0,   -- 0 | 1
  last_seen        TEXT NOT NULL,                 -- ISO 8601
  UNIQUE(account_name, group_id)
);

CREATE INDEX idx_discovered_pending
  ON discovered_groups(account_name) WHERE added_to_posting=0;
```

**Ciclo de vida:**
1. INSERT or UPDATE: `upsert_discovered_group(account_name, group_id, group_name, discovered_at)`
   - Si UNIQUE conflict: actualiza last_seen y group_name (en caso que FB cambió el nombre)
   - Si nuevo: inserta con added_to_posting=0
2. UPDATE: `mark_group_added_to_posting(account_name, group_id)` → added_to_posting=1

**Retención:** Borrar grupos no vistos en >180 días (futuro admin task)

---

## Manejo de errores

### Errores previstos y recuperación

| Error | Causa probable | Código | Recuperación |
|-------|----------------|--------|--------------|
| 404 (Cuenta no encontrada) | Nombre typo o borrada | API | Admin revisa nombre |
| 404 (run_id no encontrado) | run_id expiró o typo | Polling | Reintentar trigger |
| timeout | FB tardó >30s en responder | _run_discovery | Status=failed, retry manual |
| "Navigation timeout" | Mala conexión a FB | _run_discovery | Status=failed, retry manual |
| "Element not found" | DOM cambió | _run_discovery | Status=failed, actualizar script |
| "too many requests" | Rate limit de FB | _run_discovery | Status=failed, esperar, retry |

**Todos los errores:**
- Se capturan en try-except de `_run_discovery()`
- Se guardan en DB (primeros 500 chars)
- Se loguean a ERROR
- El admin ve toast rojo con detalles
- No lanzan ni bloquean endpoints

---

## Logging

### Log entries por fase

**Trigger (POST endpoint):**
```
INFO: Descubrimiento iniciado para 'maria' (run=a1b2c3d4e5f6)
```

**Thread _run_discovery():**
```
INFO: [maria] Iniciando descubrimiento de grupos (run=a1b2c3d4e5f6)
INFO: [maria] Navegando a groups feed...
DEBUG: [maria] Scroll 1/10...
DEBUG: [maria] Scroll 4/10...
DEBUG: [maria] Scroll 7/10...
INFO: [maria] Ejecutando script de extracción...
INFO: [maria] Descubrimiento completado: 14 grupos (run=a1b2c3d4e5f6)
```

O si falla:
```
ERROR: [maria] Error en descubrimiento (run=a1b2c3d4e5f6)
ERROR: [maria] Traceback...
```

### Logs en archivos

- `logs/main.log` — Logs globales (trigger, helper)
- `logs/maria.log` — Logs de cuenta (discover_groups_for_account)

---

## Seguridad y límites

**Rate limiting:**
- No hay rate limiter específico para discovery
- Usa el del servidor general (10 req/60s por IP)
- Admin panel requiere autenticación de sesión

**Datos sensibles:**
- `group_id` y `group_name` son públicos (no exponen credenciales)
- `discovered_groups.added_to_posting` es flag local (no se expone al grupo)
- Error messages limitados a 500 chars (evita logs gigantes)

**Concurrencia:**
- Un run por cuenta es lo esperado (no hay locks específicos)
- Si admin clickea 🔍 dos veces: se crean dos runs simultáneos (OK, son independientes)
- Si dos cuentas descubren en paralelo: OK (threads separados, locks de DB manejan acceso)

---

## Ejemplo cURL (testing)

### Trigger
```bash
curl -X POST http://localhost:5000/admin/api/accounts/maria/discover-groups \
  -H "Cookie: session=YOUR_SESSION_COOKIE"
```

Respuesta:
```json
{"run_id":"a1b2c3d4e5f6","status":"running"}
```

### Polling
```bash
curl -X GET http://localhost:5000/admin/api/discovery/a1b2c3d4e5f6 \
  -H "Cookie: session=YOUR_SESSION_COOKIE"
```

Respuesta (después de 20 segundos):
```json
{
  "id":"a1b2c3d4e5f6",
  "account_name":"maria",
  "status":"done",
  "started_at":"2026-04-25T14:32:10.123456",
  "finished_at":"2026-04-25T14:32:45.654321",
  "groups_found":14,
  "error":null
}
```

### Listar descubiertos
```bash
curl -X GET http://localhost:5000/admin/api/accounts/maria/discovered-groups \
  -H "Cookie: session=YOUR_SESSION_COOKIE"
```

### Añadir grupo
```bash
curl -X POST http://localhost:5000/admin/api/accounts/maria/discovered-groups/123456789/add \
  -H "Cookie: session=YOUR_SESSION_COOKIE"
```

---

## Debugging

### Verificar en DB

```sql
-- Ver todos los runs
SELECT * FROM discovery_runs ORDER BY started_at DESC LIMIT 10;

-- Ver grupos descubiertos de una cuenta
SELECT * FROM discovered_groups WHERE account_name='maria' ORDER BY discovered_at DESC;

-- Ver solo pendientes
SELECT * FROM discovered_groups WHERE account_name='maria' AND added_to_posting=0;

-- Ver únicamente error de un run
SELECT id, status, error FROM discovery_runs WHERE id='a1b2c3d4e5f6';
```

### Verificar en logs

```bash
tail -50 facebook_auto_poster/logs/main.log
tail -50 facebook_auto_poster/logs/maria.log
grep "discovery" facebook_auto_poster/logs/*.log
```

---

## Notas

- **Polling cada 3 segundos** fue elegido como balance entre responsividad y carga de servidor
- **500 caracteres de error** evita truncaciones frecuentes en UX y logs, pero previene gigantismo
- **Tablas separadas** (discovery_runs vs discovered_groups) permite auditoria independiente
- **marked_group_added_to_posting en dos sitios** (accounts.groups + discovered_groups.added_to_posting) proporciona redundancia y es intencionado
