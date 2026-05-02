# Verificacion de Bugs — Sistema de Proxies & Tunel

> Rama: `produccion_temp`
> Proposito: Checklist para verificar si los bugs corregidos aqui tambien estan presentes en la otra rama.

Cada bug incluye: sintoma observable, como verificar si existe en la otra rama, y referencia del fix aplicado.

---

## P1 — `--status` del script falla por falta de PySocks

- **Archivo:** `setup_phone_proxy.sh`
- **Sintoma:** Al ejecutar `./setup_phone_proxy.sh --status` aparece: `Missing dependencies for SOCKS support`. El script usa `python3` del sistema, que no tiene `PySocks` instalada.
- **Verificacion:** Ejecutar `./setup_phone_proxy.sh --status`. Si falla con el error de dependencias SOCKS, el bug esta presente.
- **Fix aplicado:** Auto-detectar el Python del venv al inicio del script (`_pick_python()`) buscando en `$SCRIPT_DIR/../.venv/bin/python3` y `$HOME/Proyectos/.venv/bin/python3`. Reemplazar todos los `python3` por `$PYTHON`.
- **Archivos modificados:** `setup_phone_proxy.sh`, `requirements.txt` (agregar `PySocks~=1.7`)

---

## P2 — Race condition en `assign_proxy_to_account()` sin lock

- **Archivo:** `proxy_manager.py`
- **Sintoma:** Dos threads ejecutando `assign_proxy_to_account()` simultaneamente pueden asignar dos cuentas al mismo nodo sin detectar el solapamiento.
- **Verificacion:** Buscar si existe `_assign_lock = threading.Lock()` y si `assign_proxy_to_account()` esta envuelta en `with _assign_lock:`.
- **Fix aplicado:** Declarar `_assign_lock = threading.Lock()` globalmente. Envolver toda la logica de lectura-nodos + calculo-score + asignacion en `with _assign_lock:`.
- **Archivos modificados:** `proxy_manager.py`

---

## P3 — `_check_node()` ignora errores de JSON

- **Archivo:** `proxy_manager.py`
- **Sintoma:** Si el proxy devuelve HTTP 200 pero con body HTML (pagina de error del ISP), `resp.json()` lanza excepcion que cae al `except Exception` generico. El nodo se marca offline cuando en realidad el proxy si conecta.
- **Verificacion:** Buscar si `_check_node()` tiene `except requests.Timeout` y `except requests.ConnectionError` separados. Si todo cae en un solo `except Exception`, el bug esta presente.
- **Fix aplicado:**
  - Separar `except requests.Timeout`, `except requests.ConnectionError`, `except Exception`
  - Validar `resp.status_code == 200` antes de parsear JSON
  - Validar JSON con su propio try/except que loguea `warning` en vez de marcar offline
- **Archivos modificados:** `proxy_manager.py`

---

## P4 — `admin_assign_proxy()` no valida que los nodos existan

- **Archivo:** `api_server.py`
- **Sintoma:** En la asignacion manual de proxy, si `primary_node` o `secondary_node` no existen en BD, la funcion falla con FK constraint error sin retornar un mensaje util al cliente.
- **Verificacion:** Intentar asignar via API un nodo inexistente. Si el error es un 500 generico en vez de 404 con mensaje claro, el bug esta presente.
- **Fix aplicado:** Validar `primary` y `secondary` con `job_store.get_proxy_node()` antes de `set_proxy_assignment()`. Retornar 404 con mensaje si no existen. Envolver en try/except para errores de BD.
- **Archivos modificados:** `api_server.py`

---

## P5 — `resolve_proxy()` sin cache ni validacion de frescura

- **Archivo:** `proxy_manager.py`
- **Sintoma:** Cada llamada a `resolve_proxy()` lee la BD. No hay cache, y el estado del nodo puede estar desactualizado (health checker corre cada 2 min).
- **Verificacion:** Buscar `_proxy_cache` y `_PROXY_CACHE_TTL_S` en `proxy_manager.py`. Si `resolve_proxy()` consulta la BD en cada llamada sin TTL, el bug esta presente.
- **Fix aplicado:**
  - Cache con `_proxy_cache: dict[str, tuple[dict, float]]` y TTL de 30s
  - Validacion rapida: si `last_checked > 180s`, hacer `_check_node()` antes de retornar
  - Parametro `force_refresh` para ignorar cache
- **Archivos modificados:** `proxy_manager.py`

---

## P6 — `_alert_node_down()` sin error handling ni guardado en BD

- **Archivo:** `proxy_manager.py`
- **Sintoma:** Cuando un nodo cae, solo se loguea en consola. No hay notificacion centralizada ni registro persistente para el dashboard.
- **Verificacion:** Buscar si `_alert_node_down()` tiene try/catch alrededor de `get_accounts_for_node()` y si guarda alertas en BD.
- **Fix aplicado:**
  - Try/catch en `get_accounts_for_node()` (no rompe si falla)
  - Guardar alerta en BD via `job_store.create_system_alert(alert_msg, severity="critical")`
  - Manejar `names` vacios si falla la consulta
- **Archivos modificados:** `proxy_manager.py`

---

## P7 — Sin validacion de formato `server` URL

- **Archivo:** `api_server.py` — endpoints de creacion/edicion de nodos proxy
- **Sintoma:** Solo valida que `server` empiece con `socks5://`, `http://`, `https://`. No valida host, puerto ni caracteres invalidos en URL.
- **Verificacion:** Buscar funcion `_validate_proxy_url()` que use `urlparse` para validar esquema, hostname y rango de puerto (1-65535).
- **Fix aplicado:**
  - `_validate_proxy_url(server)` usando `urllib.parse.urlparse`
  - Validacion de esquema (`socks5`, `http`, `https`), hostname presente, puerto en rango 1-65535
- **Archivos modificados:** `api_server.py`

---

## P8 — Sin validacion de `node_id` contra palabras reservadas

- **Archivo:** `api_server.py`
- **Sintoma:** Solo valida `^[a-z0-9_]{1,40}$` pero no bloquea palabras como "online", "offline", "maintenance" que podrian causar conflictos.
- **Verificacion:** Buscar `RESERVED_NODE_IDS` y validacion de nombres reservados en el endpoint de creacion/edicion de nodos.
- **Fix aplicado:**
  - Definir `RESERVED_NODE_IDS = {"online", "offline", "maintenance"}`
  - Validar que `node_id` no este en la lista de reservados
- **Archivos modificados:** `api_server.py`

---

## P9 — Sin limite en numero de nodos proxy (potencial DoS)

- **Archivo:** `api_server.py` — `admin_create_proxy()`
- **Sintoma:** Sin validacion de cantidad maxima de nodos. Un atacante podria crear miles de nodos.
- **Verificacion:** Buscar `MAX_PROXY_NODES` y validacion de limite en `admin_create_proxy()`.
- **Fix aplicado:** `MAX_PROXY_NODES = 1000`. Validar `len(nodes) >= MAX_PROXY_NODES` antes de crear nuevo nodo. Retornar 409 si se alcanza el limite.
- **Archivos modificados:** `api_server.py`

---

## P10 — `_read_static_url()` falla si archivo de tunel esta vacio

- **Archivo:** `main.py`
- **Sintoma:** `_read_static_url()` hace `_URL_FILE.read_text().strip()` sin verificar si el archivo existe o tiene contenido valido. Si esta vacio, `url = ""` y los webhook callbacks fallan silenciosamente.
- **Verificacion:** Buscar si `_read_static_url()` retorna `str | None` y valida que la URL empiece con `http://` o `https://`.
- **Fix aplicado:**
  - Retornar `None` si el archivo no existe
  - Validar que el contenido no este vacio y empiece con `http://` o `https://`
  - Log `ERROR` si el archivo tiene contenido invalido
- **Archivos modificados:** `main.py`

---

## P11 — `_read_backend()` no valida el valor del tunel

- **Archivo:** `main.py`
- **Sintoma:** `_read_backend()` lee el archivo sin validar que el valor sea `"cloudflare"` o `"ngrok"`. Un valor incorrecto pasaria desapercibido.
- **Verificacion:** Buscar si `_read_backend()` retorna `str | None` y valida contra `("cloudflare", "ngrok")`.
- **Fix aplicado:**
  - Retornar `None` si el archivo no existe
  - Validar que el contenido sea exactamente `"cloudflare"` o `"ngrok"`
  - Log `ERROR` si el valor es invalido
- **Archivos modificados:** `main.py`

---

## Mejora: Sistema de asignacion dinamica LRU

No es un bug, pero es una mejora significativa que el otro equipo debe conocer:

- **Archivos:** `proxy_manager.py`, `job_store.py`
- **Descripcion:** Asignacion automatica de proxies cuando una cuenta no tiene uno asignado. Si todos los nodos estan llenos, expulsa la cuenta con `last_used_at` mas antiguo (LRU).
- **Componentes:**
  - `MAX_ACCOUNTS_PER_NODE = 10` — capacidad por telefono
  - `touch_proxy_assignment()` — actualiza `last_used_at` al publicar
  - `count_accounts_for_node()` — verifica espacio libre
  - `get_lru_account_for_node()` — candidata a expulsion
  - `_assign_to_free_slot()` — busca nodo con espacio
  - `_evict_lru_and_assign()` — expulsa LRU y reasigna
- **Verificacion:** Buscar estas funciones en `proxy_manager.py`. Si `resolve_proxy()` solo lee asignaciones existentes sin logica de asignacion dinamica, la mejora no esta presente.
- **Archivos modificados:** `proxy_manager.py`, `job_store.py` (columna `last_used_at` en `account_proxy_assignment`)

---

## P12 — USB tethering consume datos del SIM (ruta por defecto)

- **Archivos:** `proxy_manager.py`, `proxy_cli.py`
- **Sintoma:** Al conectar un telefono por USB, NetworkManager crea automaticamente un perfil con `never-default=no`. La metrica de la ruta USB (100) es menor que WiFi (600), por lo que **todo el trafico del sistema** pasa por los datos del SIM. La IP "directa" y la IP "proxy" muestran el mismo valor (WiFi bypass falso).
- **Causa:** Cada vez que se conecta un telefono con una MAC diferente (o mismo telefono en distinto puerto USB), NM crea un perfil nuevo sin `never-default`. El dispatcher script solo corrige reactivamente.
- **Verificacion:** 
  ```bash
  ip route show default
  # Si aparece "dev enx*" con metrica < 600, el bug esta presente
  nmcli -t -f ipv4.never-default connection show "Conexión cableada X"
  # Si dice "no", el bug esta presente
  ```
- **Fix aplicado:**
  1. `proxy_manager._ensure_usb_never_default()` — funcion que corre al arrancar y cada 120s en el health checker:
     - Detecta todas las interfaces `enx*/usb*/rndis*/enu*` con estado UP/UNKNOWN
     - Para cada una, verifica `never-default=yes` en su perfil NM
     - Si no, lo corrige con `nmcli modify` + `nmcli device reapply`
     - Bloquea DNS por USB con `resolvectl domain <iface> ~.`
  2. `proxy_cli.py setup` tambien ejecuta esta proteccion al agregar un telefono nuevo
- **Archivos modificados:** `proxy_manager.py` (`_ensure_usb_never_default`, import `subprocess`, `re`), `proxy_cli.py` (`_ensure_never_default` con `nmcli device reapply`)

---

## P13 — `setup_phone_proxy.sh` reemplazado por `proxy_cli.py`

- **Archivos:** `proxy_cli.py` (nuevo, 558 lineas), `setup_phone_proxy.sh` (deprecated)
- **Descripcion:** El script bash de 1027 lineas con wizard interactivo de 6 pasos fue reemplazado por un CLI en Python puro que usa `job_store` y `proxy_manager` directamente.
- **Comandos:**
  | Comando | Funcion |
  |---------|---------|
  | `proxy_cli.py setup` | Auto-detectar telefono → configurar NM → escanear proxy → registrar en DB → auto-asignar cuentas |
  | `proxy_cli.py status` | Mostrar nodos + IP publica + cuentas asignadas |
  | `proxy_cli.py test` | Probar conectividad sin modificar DB |
  | `proxy_cli.py fix NODE` | Re-detectar IP del telefono si cambio |
  | `proxy_cli.py assign NODE CUENTA` | Asignacion manual |
  | `proxy_cli.py unassign CUENTA` | Quitar asignacion |
- **Verificacion:** Ejecutar `.venv/bin/python proxy_cli.py status`. Si funciona, el sistema esta actualizado.
- **Archivos modificados:** `proxy_cli.py` (nuevo), `setup_phone_proxy.sh` (marcado deprecated en header)

---

## Nota: `_ensure_usb_never_default` requiere PolicyKit o permisos NM

En Ubuntu con NetworkManager, los usuarios locales pueden modificar conexiones sin autenticacion. Si en otro sistema `nmcli modify` pide contraseña, agregar regla PolicyKit:

```bash
# /etc/polkit-1/localauthority/50-local.d/10-nm-own.pkla
[Allow user to modify own connections]
Identity=unix-user:angel
Action=org.freedesktop.NetworkManager.settings.modify.own
ResultAny=yes
```
