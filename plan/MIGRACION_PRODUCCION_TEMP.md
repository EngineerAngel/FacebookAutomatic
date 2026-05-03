# Plan de Migración: `produccion_temp` → `fase-3`

**Fecha:** 2026-05-01  
**Ancestro común:** `5bf6526` (feat(2.11): proxies)  
**Estrategia:** Migración por bloques temáticos, adaptando código sync a la arquitectura async de fase-3.

---

## Reglas estrictas de sesión

1. **Solo implementar lo pedido explícitamente** — nada más. Si se encuentra algo "mejorable" fuera del alcance, reportarlo como nota al final, no implementarlo.
2. **Antes de crear un archivo nuevo**, confirmar que no existe ya uno similar.
3. **Antes de modificar un archivo existente**, leer su contenido completo.
4. **No refactorizar** código que no esté en el alcance de la tarea.
5. **No agregar** imports, dependencias o funciones auxiliares no solicitadas.
6. **Si una tarea requiere tocar más archivos de los esperados**, pausar y reportar antes de continuar.
7. **Ser crítico con los cambios** — `produccion_temp` puede tener errores escondidos. No asumir que su código es correcto.

## Filosofía de cambio

1. **Mapear el impacto** antes de tocar código — identificar todos los consumidores: ruta → vista → JS cliente.
2. **Cambiar de adentro hacia afuera** — primero DB/modelo, luego backend, luego vista, luego JS.
3. **Un cambio por commit** — cada bloque va en su propio commit. Facilita `git revert` quirúrgico.
4. **Verificar el contrato de API antes y después** — anotar qué devuelve cada función actualmente y qué se espera que devuelva.
5. **Probar el flujo completo** — si cambio una función, verificar que los otros consumidores no se rompieron.

---

## Resumen ejecutivo

`produccion_temp` tiene features de UX/producto más completos; `fase-3` tiene mejor infraestructura técnica.
El objetivo es traer los features de producto a fase-3 **sin degradar** la arquitectura async, métricas Prometheus, WAL SQLite ni FastAPI.

### Qué NO se migra (y por qué)

| Elemento | Razón |
|----------|-------|
| `facebook_poster.py` (sync) | fase-3 es async-only — eliminado intencionalmente |
| `threading.Lock` en job_store | fase-3 usa SQLite WAL que es superior |
| `main.py` restructuring | fase-3 tiene mejor modularización con `api_main.py` |
| Setup scripts `.sh` | No es código de aplicación |
| Prometheus + Grafana | Ya está en fase-3 (Paso 3.3b) — no está en produccion_temp |
| Tabla `selector_repairs` | Solo en fase-3 (Paso 3.4) — no retroceder |
| Modo biproceso (`split_processes`) | Solo en fase-3 (Paso 3.7) — no retroceder |
| Sistema de proxies complejo | Analizar por separado — alto riesgo |

---

## Bloques de migración

### Bloque 1 — Base de Datos (`job_store.py`) ✅ BAJO RIESGO

**Cambios a portar:**

1. **Helpers multi-imagen** — `_decode_image_paths()` y `_encode_image_paths()` (líneas 27–48 en produccion_temp)
   - Toleran JSON array o string legacy (backward compat)
   
2. **Tabla `templates`** — schema completo:
   ```sql
   CREATE TABLE IF NOT EXISTS templates (
     id TEXT PRIMARY KEY,
     name TEXT UNIQUE NOT NULL,
     text TEXT NOT NULL,
     url TEXT NOT NULL DEFAULT '',
     image_path TEXT,
     created_at TEXT NOT NULL
   );
   CREATE INDEX IF NOT EXISTS idx_templates_created ON templates(created_at DESC);
   ```
   
3. **Migración `group_ids`** en tabla `jobs`:
   ```sql
   ALTER TABLE jobs ADD COLUMN group_ids TEXT
   ```
   
4. **`create_job()` signature actualizado**:
   - `image_paths: list[str] | None` (en vez de `image_path: str | None`)
   - `group_ids: dict[str, list[str]] | None = None`
   
5. **`pop_due_scheduled()` retorno actualizado**:
   - Retorna `image_paths` (list) y `group_ids` (dict|None)
   
6. **CRUD templates** (funciones nuevas):
   - `create_template(name, text, url, image_paths) → str`
   - `list_templates() → list[dict]`
   - `get_template(template_id) → dict | None`
   - `update_template(template_id, name, text, url, image_paths) → bool`
   - `delete_template(template_id) → bool`

**Riesgo de regresión:** BAJO — cambios aditivos (nueva tabla, nuevas funciones, nueva columna). `image_path` legacy se decodifica transparentemente.

---

### Bloque 2 — Config (`config.py`) ✅ BAJO RIESGO

**Cambios a portar:**

1. **`apply_group_filter(accounts, group_ids)`** — función nueva completa:
   - `None` → sin filtro (backward compat)
   - `{}` → ninguna cuenta (retorna lista vacía)
   - Cuenta ausente en dict → se omite
   - Intersección: solo grupos que la cuenta tiene configurados

**Riesgo de regresión:** NULO — función nueva, no modifica nada existente.

---

### Bloque 3 — API Server (`api_server.py`) ⚠️ MEDIO RIESGO

**Subcambios ordenados por riesgo:**

#### 3a — Constantes y helpers (BAJO)
- `_MAX_IMAGES = 5`
- Constantes de templates: `MAX_TEMPLATE_NAME_CHARS`, `MIN_TEMPLATE_TEXT_CHARS`, `MAX_TEMPLATE_TEXT_CHARS`, `_TEMPLATE_ID_PATTERN`
- `_safe_image_path(path)` helper actualizado (ya existe, verificar compatibilidad)
- `_safe_image_paths(value) → tuple[list[str]|None, str|None]` — función nueva

#### 3b — Validación mejorada de grupos (BAJO)
- `_is_valid_group_id()` — acepta IDs numéricos (8–20 dígitos) O slugs alfanuméricos; rechaza repetición total

#### 3c — Multi-foto en `_extract_payload()` (MEDIO)
- Acepta `request.files.getlist("image")` (múltiples archivos)
- Acepta `image_paths` JSON (reutilizar imágenes ya subidas)
- Backward compat con `image_path` singular
- `group_ids` JSON opcional en payload

#### 3d — `_run_job()` y `_schedule_job()` (MEDIO)
- Pasar `image_paths: list[str]` en lugar de `image_path: str`
- Pasar `group_ids` al job
- Llamar `apply_group_filter()` antes de ejecutar

#### 3e — Templates CRUD endpoints (BAJO — autónomo)
- `GET /admin/api/templates`
- `POST /admin/api/templates`
- `GET /admin/api/templates/<id>`
- `PUT /admin/api/templates/<id>`
- `DELETE /admin/api/templates/<id>`
- `POST /admin/api/upload-images` (subida independiente)
- `GET /admin/uploaded-images/<file>` (sirve imágenes para preview)

#### 3f — Login manual adaptado a async (ALTO — requiere adaptación)
- `_run_login()` en produccion_temp usa `FacebookPoster` (sync) → **adaptar a `FacebookPosterAsync` + `asyncio.run()`**
- Endpoints: `POST /admin/api/accounts/<name>/login` y `GET .../login/<run_id>`

#### 3g — Bug fixes de discovery (BAJO)
- `admin_trigger_discovery()` y `admin_list_discovered_groups()` usan `load_accounts()` en fase-3 → corregir a `list_accounts_full()` (como en produccion_temp)
- Fix: credenciales same-origin en fetch (produccion_temp línea ~821)

---

### Bloque 4 — `facebook_poster_async.py` ⚠️ MEDIO RIESGO

**Cambios a portar:**

1. **Firma de `publish_to_all_groups()`**:
   - `image_path: str | None` → `image_paths: list[str] | None`
   - Backward compat: si recibe string, convertir a lista de 1

2. **Publicación multi-imagen** (carrusel Facebook):
   - Subir cada imagen usando el selector de "agregar foto"
   - Verificar que el flujo async funcione con múltiples archivos

**Riesgo de regresión:** MEDIO — cambio de firma propagado. Requiere actualizar todas las llamadas en `account_manager_async.py`.

---

### Bloque 5 — UI Templates ⚠️ ALTO RIESGO

**admin.html:**
- Tab "Plantillas" con CRUD completo
- Botón 🔑 "Iniciar sesión" por cuenta con polling de estado

**publish.html:**
- Selector de plantillas ("Usar plantilla")
- Vista previa de imágenes múltiples
- Upload de hasta 5 imágenes

**Riesgo:** ALTO por complejidad de JS/HTML. Verificar que no rompan las funcionalidades existentes (métricas tab de fase-3, etc.).

---

## Orden de ejecución recomendado

```
Bloque 1 (job_store)     → sin riesgo de regresión
Bloque 2 (config)        → sin riesgo de regresión  
Bloque 3a-3b (helpers)   → sin riesgo de regresión
Bloque 3c-3d (payload)   → riesgo medio, testear bien
Bloque 4 (async poster)  → riesgo medio, requiere test funcional
Bloque 3e (templates API)→ autónomo, riesgo bajo
Bloque 3f (login manual) → adaptación async, riesgo alto
Bloque 3g (bug fixes)    → quick wins
Bloque 5 (UI)            → al final, más complejo
```

---

## Estado de implementación

| Bloque | Estado | Commit |
|--------|--------|--------|
| 1a — DB: helpers + tabla templates + group_ids | ✅ hecho | `7d0e975` |
| 1b+1c — DB: create_job/pop_due multi-imagen | ✅ hecho (incluido en 3c+3d) | `2306846` |
| 2 — Config (apply_group_filter) | ✅ hecho | `b12e377` |
| 3a — Constantes y helpers | ✅ hecho | `cf8edd9` |
| 3b — Validación grupos | ✅ hecho | `c581813` |
| 3c — Multi-foto payload | ✅ hecho | `2306846` |
| 3d — _run_job multi-foto | ✅ hecho | `2306846` |
| 3e — Templates CRUD API | ✅ hecho | `797101e` |
| 3f — Login manual async | ✅ hecho | `d374f92` |
| 3g — Bug fixes discovery | ✅ hecho | `f549945` |
| 4 — poster async multi-foto | ✅ hecho | `3e9c5ad` |
| 5a — Botón login manual en admin.html | ✅ hecho | `(incluido en 5b)` |
| 5b — Tab Plantillas en admin.html | ✅ hecho | (ver git log) |
| 5c — Multi-imagen en publish.html | ✅ hecho | (ver git log) |
| 5d — Selector de plantillas en publish.html | ✅ hecho | `d60b18c` |
