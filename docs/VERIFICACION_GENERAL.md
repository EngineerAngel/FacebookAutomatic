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

---

## G11 — `skip_hour_check` para publicaciones manuales desde admin

- **Archivos:** `account_manager.py`, `api_server.py`
- **Descripcion:** Cuando un admin publica manualmente desde `/admin/publish`, no debe bloquearse por la ventana horaria de la cuenta. El guard de horario es para jobs automaticos (OpenClaw), no para acciones manuales del admin.
- **Verificacion:**
  - Buscar `skip_hour_check: bool = False` en `AccountManager.__init__()` y `_run_job()`
  - Verificar que `_run_job()` pasa `skip_hour_check=True` cuando la publicacion viene del admin (endpoints `/admin/api/post` y `/admin/api/schedule`)
  - Verificar que `AccountManager.run()` evalua `skip_hour_check` antes de filtrar cuentas por horario
- **Fix aplicado:** Parametro `skip_hour_check` agregado a `AccountManager` y propagado desde `_run_job()`. Los endpoints admin lo pasan como `True`, los endpoints OpenClaw como `False` (default).
- **Archivos modificados:** `account_manager.py`, `api_server.py`

---

## G12 — Resultados de publicacion muestran desglose por grupo

- **Archivos:** `api_server.py`, `templates/publish.html`, `templates/admin.html`
- **Descripcion:** Antes solo se mostraba "Publicado exitosamente" o "Error". Ahora el resultado incluye detalle: grupos OK, grupos fallidos, y el tag de cada grupo.
- **Verificacion:**
  - Buscar `group-results-detail` en `publish.html` (renderiza resultados por grupo)
  - Buscar `groups_ok`, `groups_fail` en `api_server.py` — el endpoint de job status los incluye en la respuesta
  - Buscar `loadJobDetail()` en `publish.html` (polling mejorado con detalle)
- **Fix aplicado:**
  - `api_server.py`: `_run_job()` persiste `groups_ok` y `groups_fail` en `job_results`
  - `templates/publish.html`: renderizado de resultados agrupados con badges OK/ERROR + tags
  - `templates/admin.html`: columna de resultados en tabla de jobs con conteo OK/FAIL
- **Archivos modificados:** `api_server.py`, `templates/publish.html`, `templates/admin.html`

---

## G13 — Seleccion de grupos especificos ignorada (pipeline incompleto)

- **Archivos:** `publish.html`, `api_server.py`, `scheduler_runner.py`, `job_store.py`, `config.py`
- **Sintoma:** El usuario selecciona grupos especificos en el paso 3 de `/admin/publish` (checkboxes por cuenta), pero al publicar el sistema ignora la seleccion y publica en TODOS los grupos de la cuenta. La UI existe, `apply_group_filter()` existe, la columna `group_ids` en BD existe — pero nunca se conectaron.
- **Causa:** El pipeline se construyo en fases separadas y nunca se completo. El commit original (`690e151`) creo `state.selectedGroups` en el frontend. El commit `a06ff19` agrego el TODO `// TODO: Enviar grupos especificos`. La funcion `apply_group_filter()` en `config.py` y la columna `group_ids` en `jobs` se agregaron despues, pero nadie las conecto.
- **Verificacion:**
  - En `publish.html`, buscar si `publish()` envia `state.selectedGroups` en el FormData. Si solo hay un TODO, el bug esta presente.
  - En `api_server.py`, buscar si `_extract_payload()` parsea `group_ids`.
  - En `api_server.py`, buscar si `_run_job()` aplica `apply_group_filter()`.
  - En `scheduler_runner.py`, buscar si `_run_scheduled_job()` lee `job["group_ids"]`.
  - En `job_store.py`, buscar si `get_job()` incluye `group_ids` en el SELECT.
- **Fix aplicado (5 archivos, ~40 lineas):**
  1. `publish.html`: `fd.append('group_ids', JSON.stringify(state.selectedGroups))`
  2. `api_server.py`: `_extract_payload()` parsea `group_ids` de multipart y JSON
  3. `api_server.py`: `_run_job()` acepta `group_ids`, llama `apply_group_filter()` antes de `AccountManager`
  4. `api_server.py`: `_enqueue_job()` y los 4 call sites de `create_job()` pasan `group_ids`
  5. `scheduler_runner.py`: importa `apply_group_filter`, la aplica en `_run_scheduled_job()`
  6. `job_store.py`: `get_job()` y `get_recent_jobs()` incluyen `group_ids` en SELECT
- **Archivos modificados:** `publish.html`, `api_server.py`, `scheduler_runner.py`, `job_store.py`

---

## G14 — `apply_group_filter` ignora el filtro para cuentas ausentes del dict

- **Archivo:** `config.py` — `apply_group_filter()`
- **Sintoma:** El usuario selecciona cuentas A y B en el paso 2, pero solo marca grupos para A. El frontend envia `group_ids = {"A": ["g1"]}`. La cuenta B no aparece en el dict. `apply_group_filter()` la trata como "sin filtro" y la devuelve con todos sus grupos (hasta 167). Resultado: B publica en todos sus grupos aunque el usuario no lo indico.
- **Causa:** `if selected is None: result.append(acc)` — ausencia en el dict se interpretaba como "no filtrar esta cuenta" en lugar de "esta cuenta no fue seleccionada".
- **Verificacion:**
  - Buscar en `config.py` el bloque `if selected is None:`. Si hace `result.append(acc)`, el bug esta presente.
  - Verificar con: `apply_group_filter([a, b], {"a": ["g1"]})` — si devuelve ambas cuentas, el bug esta presente.
- **Fix aplicado:**
  ```python
  # ANTES — cuenta ausente hereda todos sus grupos
  if selected is None:
      result.append(acc)
  # DESPUES — cuenta ausente se omite
  if selected is None:
      continue
  ```
- **Archivos modificados:** `config.py`

---

## G15 — `if not group_ids:` trata dict vacio `{}` igual que `None`

- **Archivo:** `config.py`, `api_server.py`, `scheduler_runner.py`
- **Sintoma:** Si se envia `group_ids = {}` (dict vacio), las guardas `if not group_ids:` y `if group_ids:` lo evaluan como falso — exactamente igual que `None`. El filtro se salta y el sistema publica en todos los grupos. Tambien afecta el string de log que dice "no" cuando deberia decir "si" (o viceversa).
- **Causa:** Python evalua `bool({}) == False`, por lo que `if not {}` entra al bloque "sin filtro". La distincion semantica entre "no se envio group_ids" (`None`) y "se envio vacio" (`{}`) se pierde.
- **Verificacion:**
  - En `config.py`: buscar `if not group_ids:`. Si no usa `is None`, el bug esta presente.
  - En `api_server.py` (`_run_job`): buscar `if group_ids:`. Si no usa `is not None`, el bug esta presente.
  - En `scheduler_runner.py` (`_run_scheduled_job`): igual.
- **Fix aplicado:**
  ```python
  # config.py
  # ANTES
  if not group_ids:
      return accounts
  # DESPUES
  if group_ids is None:   # None = no enviado = sin filtro (backward compat)
      return accounts
  # {} = enviado vacio = ninguna cuenta → devuelve []

  # api_server.py y scheduler_runner.py
  # ANTES
  if group_ids:
      accounts = apply_group_filter(accounts, group_ids)
  # DESPUES
  if group_ids is not None:
      accounts = apply_group_filter(accounts, group_ids)
  ```
- **Archivos modificados:** `config.py`, `api_server.py`, `scheduler_runner.py`

---

## G16 — Frontend envia mapa parcial de grupos y no valida seleccion incompleta

- **Archivo:** `templates/publish.html`
- **Sintoma (1 — mapa parcial):** `publish()` construia `selGroups` a partir de `state.selectedGroups`, que solo contiene cuentas con las que el usuario interactuo (toco al menos un checkbox). Si el usuario selecciono cuentas A y B pero solo toco checkboxes de A, B no aparece en `selGroups`. El frontend envia `group_ids = {"A": [...]}` sin B. El backend (bug G14) entonces publicaba a todos los grupos de B.
- **Sintoma (2 — hasSelection falso al desmarcar):** Si el usuario marcaba un grupo y luego lo desmarcaba, `selectedGroups[acc] = []`. `hasSelection` resultaba `false` y `group_ids` NO se enviaba en absoluto — el backend publicaba a todos los grupos por defecto.
- **Sintoma (3 — sin validacion):** El boton "Siguiente" del paso 3 no verificaba que cada cuenta seleccionada tuviera al menos 1 grupo marcado. El usuario podia llegar al paso 5 y publicar sin haber seleccionado ningun grupo.
- **Verificacion:**
  - En `publish()`, buscar si `completeGroupMap` incluye todas las cuentas de `state.selectedAccounts`. Si usa `Object.values(selGroups)` en lugar de `state.selectedAccounts`, el bug esta presente.
  - En `nextStep()`, buscar si hay validacion al avanzar a `'schedule'`. Si `nextStep` es un passthrough sin logica, el bug esta presente.
- **Fix aplicado (3 cambios en publish.html):**
  1. `publish()` — construir mapa completo:
     ```javascript
     // ANTES — mapa parcial
     const hasSelection = Object.values(selGroups).some(arr => arr.length > 0);
     if (hasSelection) fd.append('group_ids', JSON.stringify(selGroups));

     // DESPUES — mapa con TODAS las cuentas seleccionadas
     const hasSelection = state.selectedAccounts.some(acc => (selGroups[acc] || []).length > 0);
     if (hasSelection) {
       const completeGroupMap = {};
       state.selectedAccounts.forEach(acc => {
         completeGroupMap[acc] = Array.isArray(selGroups[acc]) ? selGroups[acc] : [];
       });
       fd.append('group_ids', JSON.stringify(completeGroupMap));
     }
     ```
  2. `nextStep()` — validacion al salir del paso 3:
     ```javascript
     if (section === 'schedule') {
       // Si alguna cuenta tiene grupos marcados, TODAS deben tenerlos
       const hasAny = state.selectedAccounts.some(acc => (selGroups[acc] || []).length > 0);
       if (hasAny) {
         const sinGrupos = state.selectedAccounts.filter(acc => !(selGroups[acc] || []).length);
         if (sinGrupos.length > 0) {
           showStatus('groups-status', `La cuenta "${sinGrupos}" no tiene grupos seleccionados...`, 'error');
           return;
         }
       }
     }
     ```
  3. `updateSummary()` — desglose por cuenta en lugar de total generico:
     - Antes: `"Total de grupos: N"`
     - Despues: fila por cuenta con su conteo, o `"Todos (sin filtro)"` si no hay seleccion
  4. Eliminar monkey-patch de `DOMContentLoaded` que sobreescribia `nextStep` — la logica de `updateSummary` quedo integrada directamente en la funcion.
- **Archivos modificados:** `templates/publish.html`

---

## G17 — Handlers de logging se acumulan entre jobs (output duplicado e interleado)

- **Archivo:** `facebook_poster.py` — `FacebookPoster.__init__()` y `close()`
- **Sintoma:** Tras N jobs consecutivos para la misma cuenta, cada mensaje del log aparece N veces: la mitad con el formato correcto `"YYYY-MM-DD ... - [poster.X] - INFO - ..."` y la otra mitad con el formato corto del root logger `"INFO:poster.X:..."`. En modo threading, los writes de multiples handlers al mismo archivo se interleaban a nivel de OS, corrompiendo lineas (p.ej. `683ca7e05ac044a2a772697bb5572026-05-01...`).
- **Causa:** `logging.getLogger("poster.carmen_carrillo")` devuelve el **mismo objeto** en cada llamada (singleton por nombre). `__init__` agregaba un `FileHandler` y un `StreamHandler` nuevos sin eliminar los anteriores. Ademas `propagate=True` (default) enviaba cada mensaje al root logger, que podia tener su propio `StreamHandler` con formato diferente.
- **Verificacion:**
  - Buscar en `FacebookPoster.__init__()` si limpia handlers antes de agregar los nuevos. Si no hay un bucle de `removeHandler` antes de `addHandler`, el bug esta presente.
  - Reproducir: crear dos `FacebookPoster` para la misma cuenta y observar si un mensaje se escribe dos veces en el log.
  - Buscar `self.logger.propagate = False`. Si no existe, el root logger puede duplicar salida.
- **Fix aplicado:**
  ```python
  # __init__: limpiar handlers acumulados antes de agregar nuevos
  self.logger.propagate = False   # evita propagacion al root logger
  for old_h in self.logger.handlers[:]:
      old_h.close()
      self.logger.removeHandler(old_h)
  # ... agregar FileHandler y StreamHandler frescos

  # close(): cerrar y remover handlers al terminar la sesion
  for h in self.logger.handlers[:]:
      h.close()
      self.logger.removeHandler(h)
  ```
  Adicionalmente se bajo el nivel del `StreamHandler` de `DEBUG` a `INFO` — el nivel DEBUG es excesivo para consola.
- **Archivos modificados:** `facebook_poster.py`

---

## G18 — setup.sh: precedencia de operadores rompe deteccion de arquitectura arm64

- **Archivo:** `setup.sh` linea 73
- **Sintoma:** En un servidor ARM64/aarch64 (p.ej. Raspberry Pi, AWS Graviton) se descargaba `cloudflared-linux-amd64` en lugar de `cloudflared-linux-arm64`, causando un binario incompatible que falla al ejecutarse.
- **Causa:** `A || B && C` en bash se parsea como `A || (B && C)`. Cuando `A` (`$ARCH = "arm64"`) es verdadero, la subexpresion `B && C` nunca se evalua y `ARCH_DEB` nunca se asigna a `"arm64"`.
- **Verificacion:** Buscar la linea con `ARCH_DEB="arm64"`. Si usa `[ ... ] || [ ... ] && ARCH_DEB=...` sin `if/fi`, el bug esta presente.
- **Fix aplicado:**
  ```bash
  # ANTES — arm64 nunca se asigna cuando la primera condicion es verdadera
  [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ] && ARCH_DEB="arm64"
  # DESPUES
  if [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ]; then ARCH_DEB="arm64"; fi
  ```
- **Archivos modificados:** `setup.sh`

---

## G19 — setup.sh: dpkg -l acepta paquetes en estado "rc" como instalados

- **Archivo:** `setup.sh` lineas 93-94
- **Sintoma:** Si `python3-xlib` o `scrot` fueron desinstalados anteriormente (estado `rc`: archivos de config presentes, binarios removidos), `dpkg -l` devuelve 0 y el script asume que estan instalados. Las dependencias de display quedan faltantes y Emunium falla al intentar mover el raton.
- **Causa:** `dpkg -l <pkg>` devuelve exit code 0 para cualquier estado conocido, incluido `rc` (removed+config). `dpkg -s <pkg>` solo devuelve 0 para paquetes completamente instalados (`ii`).
- **Verificacion:** Buscar `dpkg -l python3-xlib` o `dpkg -l scrot`. Si usa `-l` en lugar de `-s`, el bug esta presente.
- **Fix aplicado:**
  ```bash
  # ANTES
  dpkg -l python3-xlib &>/dev/null 2>&1 || PKGS_NEEDED+=("python3-xlib")
  dpkg -l scrot &>/dev/null 2>&1 || PKGS_NEEDED+=("scrot")
  # DESPUES
  dpkg -s python3-xlib &>/dev/null 2>&1 || PKGS_NEEDED+=("python3-xlib")
  dpkg -s scrot &>/dev/null 2>&1 || PKGS_NEEDED+=("scrot")
  ```
- **Archivos modificados:** `setup.sh`

---

## G20 — setup_tunnel.sh: PROJECT_DIR con ruta relativa se rompe al invocar el script desde otro directorio

- **Archivo:** `setup_tunnel.sh` linea 29
- **Sintoma:** Ejecutar el script desde un directorio distinto al raiz del proyecto (p.ej. `bash /opt/fb/setup_tunnel.sh` desde `/home/user`) hace que `PROJECT_DIR` apunte a una ruta invalida. Los archivos de configuracion que el script intenta leer/escribir no se encuentran.
- **Causa:** `dirname "$0"` devuelve la ruta tal como fue invocada (puede ser relativa). Si el CWD no es el directorio del script, la ruta resultante es invalida. Ademas `$0` no funciona correctamente cuando el script es `source`-ado.
- **Verificacion:** Buscar `PROJECT_DIR="$(dirname "$0")/..."`. Si no usa `BASH_SOURCE[0]` ni `cd ... && pwd`, el bug esta presente.
- **Fix aplicado:**
  ```bash
  # ANTES
  PROJECT_DIR="$(dirname "$0")/facebook_auto_poster"
  # DESPUES — ruta absoluta canonicalizada, funciona con source y con rutas relativas
  PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/facebook_auto_poster"
  ```
- **Archivos modificados:** `setup_tunnel.sh`

---

## G21 — setup_tunnel.sh: proceso de verificacion queda zombie sin wait tras kill

- **Archivo:** `setup_tunnel.sh` lineas ~212 y ~297
- **Sintoma:** Al hacer `kill $NGROK_PID` / `kill $CF_PID`, bash imprime `"Terminated: 15"` o `"[1]+ Terminated ngrok..."` en stdout **despues** de que el script continua, mezclando ese mensaje con la salida del siguiente paso (URL final). Visible en terminales interactivas.
- **Causa:** `kill` envia la senal pero no espera a que el proceso termine. Sin `wait $PID`, bash reporta la finalizacion del background job de forma asincrona cuando el proceso realmente muere.
- **Verificacion:** Buscar `kill $NGROK_PID` o `kill $CF_PID` sin un `wait` inmediatamente despues.
- **Fix aplicado:**
  ```bash
  kill $NGROK_PID 2>/dev/null || true
  wait $NGROK_PID 2>/dev/null || true   # ← evita mensaje asincrono de bash

  kill $CF_PID 2>/dev/null || true
  wait $CF_PID 2>/dev/null || true
  ```
- **Archivos modificados:** `setup_tunnel.sh`

---

## G22 — setup_phone_proxy.sh: validacion de NODE_ID/NODE_LABEL nunca se ejecuta (precedencia || + &&)

- **Archivo:** `setup_phone_proxy.sh` linea 547 (script deprecado, reemplazado por `proxy_cli.py`)
- **Sintoma:** Si el usuario presiona Enter sin escribir un ID de nodo, la validacion se saltea silenciosamente y el script continua con `NODE_ID=""`, insertando un nodo con ID vacio en la base de datos y corrompiendo la tabla `proxy_nodes`.
- **Causa:** Mismo patron que G18: `[ -z "$NODE_ID" ] || [ -z "$NODE_LABEL" ] && { fail; exit 1; }` se parsea como `[ -z "$NODE_ID" ] || ([ -z "$NODE_LABEL" ] && { fail; exit 1; })`. Cuando `NODE_ID` esta vacio (condicion verdadera), la segunda parte nunca se evalua y el script no sale.
- **Verificacion:** Buscar la linea con `fail "ID y etiqueta son obligatorios"`. Si usa `] || [ ] && {`, el bug esta presente.
- **Fix aplicado:**
  ```bash
  # ANTES
  [ -z "$NODE_ID" ] || [ -z "$NODE_LABEL" ] && { fail "ID y etiqueta son obligatorios"; exit 1; }
  # DESPUES
  if [ -z "$NODE_ID" ] || [ -z "$NODE_LABEL" ]; then fail "ID y etiqueta son obligatorios"; exit 1; fi
  ```
- **Archivos modificados:** `setup_phone_proxy.sh`

---

## G23 — setup_phone_proxy.sh: $ literal en f-string Python imprime caracter extra

- **Archivo:** `setup_phone_proxy.sh` linea 696, heredoc Python incrustado (script deprecado)
- **Sintoma:** El comando `show_node` imprime `"Cuentas asignadas (N):$<reset_code>"` — hay un `$` literal visible antes del codigo ANSI de reset, dejando basura visual en la terminal.
- **Causa:** En una f-string Python, `${reset}` es un `$` literal seguido de la expresion `{reset}`. El `$` no tiene significado especial en Python (a diferencia de bash). La variable `reset` se expande correctamente pero el `$` queda como texto.
- **Verificacion:** Buscar `:${reset}"` dentro del heredoc Python del script. Si el `$` esta presente, el bug esta presente.
- **Fix aplicado:**
  ```python
  # ANTES
  print(f"  {bold}Cuentas asignadas ({len(accs)}):${reset}")
  # DESPUES
  print(f"  {bold}Cuentas asignadas ({len(accs)}):{reset}")
  ```
- **Archivos modificados:** `setup_phone_proxy.sh`
