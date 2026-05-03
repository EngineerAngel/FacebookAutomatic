# ~~Contexto: Migración de proxies `produccion_temp → fase-3`~~ — COMPLETADA

> **Estado:** ✅ Todos los bloques (A-D) commiteados en `fase-3` (2026-05-03).  
> Este documento es histórico — describe lo que se hizo, no lo que queda pendiente.

**Rama de trabajo:** `fase-3`  
**Fecha del documento:** 2026-05-03  
**Commits de la migración:** `a3a7186` (setup.sh), `7ae026c` (setup_tunnel.sh), `29860ef` (setup_phone_proxy.sh), `9f2aed1` (proxy_cli.py), `980b2c5` (job_store LRU), `5938d43` (proxy_manager LRU)

---

## Reglas estrictas de sesión (NO negociables)

1. **Solo implementar lo pedido explícitamente** — nada más. Si se encuentra algo "mejorable" fuera del alcance, reportarlo como nota al final, no implementarlo.
2. **Antes de crear un archivo nuevo**, confirmar que no existe ya uno similar.
3. **Antes de modificar un archivo existente**, leer su contenido completo.
4. **No refactorizar** código que no esté en el alcance de la tarea.
5. **No agregar** imports, dependencias o funciones auxiliares no solicitadas.
6. **Si una tarea requiere tocar más archivos de los esperados**, pausar y reportar antes de continuar.
7. **Ser crítico con los cambios** — `produccion_temp` puede tener errores. No asumir que su código es correcto.

## Filosofía de cambio

1. **Mapear el impacto** antes de tocar código — identificar todos los consumidores.
2. **Cambiar de adentro hacia afuera** — primero DB/modelo, luego backend, luego UI.
3. **Un cambio por commit** — cada bloque en su propio commit. Facilita `git revert` quirúrgico.
4. **Verificar el contrato de API antes y después** — anotar qué devuelve cada función actualmente y qué se espera que devuelva.
5. **Probar el flujo completo** — si cambio una función, verificar que los otros consumidores no se rompieron.

---

## Estado actual del proyecto

### Migración `produccion_temp → fase-3` — COMPLETADA (excepto proxies)

Todos los bloques de la migración principal están en `fase-3`:

| Bloque | Commit |
|--------|--------|
| DB: templates, multi-imagen, group_ids | `7d0e975` |
| Config: apply_group_filter | `b12e377` |
| API helpers + validación grupos | `cf8edd9`, `c581813` |
| Multi-foto y group_ids en pipeline | `2306846` |
| Templates CRUD API | `797101e` |
| Login manual async | `d374f92` |
| Bug fixes discovery | `f549945` |
| Poster async multi-foto | `3e9c5ad` |
| UI: admin (login, plantillas, proxies) | `1980032`, `3743d48`, `6cb1099` |
| UI: publish (multi-imagen, selector plantillas) | `e6acb81`, `d60b18c` |

### Lo que queda: archivos de proxy

Estos archivos tienen diferencias entre `fase-3` y `produccion_temp` que NO se han migrado todavía.

---

## Pendiente: migración de archivos de proxy

### Bloque A — Bug fixes triviales en scripts `.sh` (RIESGO NULO)

#### `setup.sh` — 2 fixes

```diff
# Fix 1: precedencia de &&/|| era ambigua
- [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ] && ARCH_DEB="arm64"
+ if [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ]; then ARCH_DEB="arm64"; fi

# Fix 2: dpkg -l lista paquetes aunque no estén instalados; -s verifica estado
- dpkg -l python3-xlib &>/dev/null 2>&1 || PKGS_NEEDED+=("python3-xlib")
- dpkg -l scrot &>/dev/null 2>&1 || PKGS_NEEDED+=("scrot")
+ dpkg -s python3-xlib &>/dev/null 2>&1 || PKGS_NEEDED+=("python3-xlib")
+ dpkg -s scrot &>/dev/null 2>&1 || PKGS_NEEDED+=("scrot")
```

#### `setup_tunnel.sh` — 3 fixes

```diff
# Fix 1: usar BASH_SOURCE para ruta absoluta confiable
- PROJECT_DIR="$(dirname "$0")/facebook_auto_poster"
+ PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/facebook_auto_poster"

# Fix 2 y 3: wait tras kill previene procesos zombie
# (en dos lugares: uno para $NGROK_PID, otro para $CF_PID)
  kill $NGROK_PID 2>/dev/null || true
+ wait $NGROK_PID 2>/dev/null || true

  kill $CF_PID 2>/dev/null || true
+ wait $CF_PID 2>/dev/null || true
```

**Acción:** Portar directamente. Commit independiente por archivo.

---

### Bloque B — `setup_phone_proxy.sh` ampliado (RIESGO BAJO)

`produccion_temp` tiene 1035 líneas vs 579 en `fase-3`. El propio script está marcado como:
```
# ⚠ DEPRECATED — Usar proxy_cli.py en su lugar
```

**Diferencias clave:**
- Nota de depreciación en el header que redirige a `proxy_cli.py`
- Comandos nuevos: `--info`, `--edit`, `--remove`, `--assign`, `--unassign`, `--fix`
- Funciones nuevas: `info_node`, `edit_node`, `remove_node`, `assign_proxy`, `unassign_proxy`, `fix_node`
- Detección de protocolo automática (SOCKS5 vs HTTP)
- Detección del Python del venv del proyecto (busca `.venv/bin/python3` antes de usar el del sistema)
- Escaneo de 9 puertos en vez de 5: `(1080 8080 8888 3128 1081 8123 1090 9050 10808)`

**Estrategia:** Reemplazar el archivo completo con la versión de `produccion_temp` — es shell puro, no afecta la aplicación.
```bash
git show produccion_temp:setup_phone_proxy.sh > setup_phone_proxy.sh
```

**Commit:** `"chore: actualizar setup_phone_proxy.sh desde produccion_temp — deprecation notice + nuevos comandos"`

---

### Bloque C — `proxy_cli.py` (NUEVO — solo existe en `produccion_temp`) (RIESGO BAJO-MEDIO)

Reemplazo Python del `.sh`. 572 líneas. Usa `job_store` y `proxy_manager` directamente.

**Comandos:**
```bash
python proxy_cli.py setup              # auto-detectar teléfono + registrar + auto-asignar
python proxy_cli.py status             # estado de nodos
python proxy_cli.py fix NODE_ID        # re-detectar IP si cambió
python proxy_cli.py assign NODE CUENTA # asignar manualmente
python proxy_cli.py unassign CUENTA    # quitar asignación
python proxy_cli.py test               # probar conectividad
```

**Ruta en produccion_temp:** `facebook_auto_poster/proxy_cli.py`

**Estrategia:** Copiar el archivo directamente.
```bash
git show produccion_temp:facebook_auto_poster/proxy_cli.py > facebook_auto_poster/proxy_cli.py
```

**Revisar antes de portar:**
- Verificar que todos los `job_store.*` que llama existan en `fase-3`
- Verificar que todos los `proxy_manager.*` que llama existan en `fase-3`
- Si faltan funciones → son parte del Bloque D

**Commit:** `"feat: agregar proxy_cli.py — CLI Python de gestión de proxies"`

---

### Bloque D — `proxy_manager.py` + `job_store.py` (RIESGO ALTO — sesión separada recomendada)

Este es el cambio más profundo. `produccion_temp` reescribió `proxy_manager.py` con asignación dinámica LRU.

#### Diferencias en `proxy_manager.py` (206 líneas → 538)

| Aspecto | fase-3 (actual) | produccion_temp |
|---------|-----------------|-----------------|
| Asignación | Manual via `assign_proxy_to_account()` | **Dinámica en `resolve_proxy()`**: auto-asigna al primer uso |
| Capacidad | Sin límite | `MAX_ACCOUNTS_PER_NODE = 10` |
| Rotación | No existe | Expulsión LRU cuando todos los nodos están llenos |
| Cooldown | No existe | `_wait_for_node_cooldown()` — espera si el nodo fue usado recientemente |
| `last_used_at` | No existe | Se actualiza en cada publicación |

**Funciones nuevas en `produccion_temp`:**
- `_online_nodes()` — lista nodos activos
- `_assign_to_free_slot()` — busca slot libre considerando MAX_ACCOUNTS_PER_NODE
- `_evict_lru_and_assign()` — expulsa cuenta más antigua y asigna la nueva
- `_ensure_assigned()` — wrapper que llama free_slot o evict_lru según haya capacidad
- `_wait_for_node_cooldown()` — espera si el nodo fue usado hace menos de N segundos
- `resolve_proxy(account_name, force_refresh=False)` — firma extendida

#### Cambios necesarios en `job_store.py`

`produccion_temp` agrega a `job_store.py`:
- Columna `last_used_at` en tabla `account_proxy_assignment` (migración automática al conectar)
- `touch_proxy_assignment(account_name)` — actualiza `last_used_at` al momento actual
- `count_accounts_for_node(node_id)` — cuenta cuentas asignadas a un nodo
- `get_lru_account_for_node(node_id)` — retorna la cuenta con `last_used_at` más antiguo
- `last_node_use(node_id, exclude_account)` — retorna `MAX(last_used_at)` de un nodo

**IMPORTANTE:** `touch_proxy_assignment()` debe llamarse desde `facebook_poster_async.py` después de abrir el contexto del browser (cuando se "usa" el proxy). Verificar exactamente dónde llamarlo.

**Estrategia para Bloque D:**
1. Leer `job_store.py` de produccion_temp para extraer solo las funciones proxy nuevas
2. Leer `proxy_manager.py` de produccion_temp completo
3. Aplicar cambios incrementalmente: primero `job_store.py`, luego `proxy_manager.py`
4. Verificar que `facebook_poster_async.py` llame `touch_proxy_assignment` en el momento correcto
5. Correr los 84 tests unitarios: `python -m pytest facebook_auto_poster/tests/unit/ -q`

---

## Orden de ejecución recomendado

```
Bloque A — setup.sh fixes         → nulo riesgo, 5 minutos
Bloque A — setup_tunnel.sh fixes  → nulo riesgo, 5 minutos
Bloque B — setup_phone_proxy.sh   → bajo riesgo, reemplazar archivo completo
Bloque C — proxy_cli.py           → bajo-medio, revisar dependencias primero
Bloque D — proxy_manager + job_store → ALTO RIESGO, sesión dedicada con test funcional
```

---

## Comandos de orientación rápida

```bash
# Ver estado del branch
git log --oneline -10

# Ver todos los archivos que difieren entre ramas (proxy y .sh)
git diff --name-only fase-3 produccion_temp

# Ver diff de un archivo específico
git diff fase-3 produccion_temp -- setup.sh
git diff fase-3 produccion_temp -- facebook_auto_poster/proxy_manager.py

# Ver archivo completo de produccion_temp sin cambiar de rama
git show produccion_temp:facebook_auto_poster/proxy_cli.py

# Correr tests unitarios
python -m pytest facebook_auto_poster/tests/unit/ -q

# Verificar imports limpios
python -c "import api_server; print('OK')"
```

---

## Archivos clave del proyecto

| Archivo | Rol |
|---------|-----|
| `facebook_auto_poster/proxy_manager.py` | Health checker daemon + resolve_proxy + assign |
| `facebook_auto_poster/job_store.py` | DB: tablas `proxy_nodes`, `account_proxy_assignment` |
| `facebook_auto_poster/facebook_poster_async.py` | Llama `resolve_proxy()` al lanzar Chromium (línea ~261) |
| `facebook_auto_poster/api_server.py` | Endpoints `/admin/api/proxies` y `/admin/api/accounts/<name>/proxy` |
| `facebook_auto_poster/main.py` | Arranca `proxy_manager.start()` como daemon |
| `setup_phone_proxy.sh` | CLI bash para setup hardware (USB tethering) |
| `facebook_auto_poster/proxy_cli.py` | CLI Python (solo en produccion_temp — pendiente de portar) |
