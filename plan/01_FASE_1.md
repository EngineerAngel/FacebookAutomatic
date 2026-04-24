# 01 — Fase 1: Stop-the-bleeding (semana 1)

> **Objetivo:** Eliminar vectores de detección inmediata. Cada cuenta debe aparentar ser un usuario único, con identidad de red y dispositivo independiente.

## Tabla de ítems

| # | Item | Prioridad | Tiempo estimado |
|---|------|-----------|-----------------|
| 1.1 | SIM hotspot pool con resiliencia | 🔴 P0 | 2 días |
| 1.2 | Password individual cifrada | 🔴 P0 | 1 día |
| 1.3 | Fingerprint variation (UA + viewport + locale + timezone) | 🔴 P0 | 1.5 días |
| 1.4 | Revertir ventana horaria + timezone por cuenta | 🔴 P0 | 0.5 días |
| 1.5 | Bajar typo rate y mejorar patrón de corrección | 🟠 P1 | 0.5 días |
| 1.6 | Migración cross-platform Windows → Ubuntu/Mac | 🟠 P1 | 1 día |

**Total estimado:** ~6.5 días hábiles.

---

## 1.1 — SIM hotspot pool con resiliencia

### Contexto de hardware disponible
- **Servidor:** casa (Ubuntu o Mac) — siempre encendido.
- **Teléfono 1:** dual SIM → 2 IPs móviles independientes.
- **Teléfonos 2, 3, 4:** 1 SIM cada uno → 3 IPs adicionales.
- **Total disponible:** 5 IPs únicas de carrier móvil.

IPs móviles son las de mayor confianza para Facebook — los carriers asignan rangos IP que Facebook asocia a comportamiento humano real.

### Problema original
Todas las cuentas salen de la misma IP. Sin esta separación, el resto de mejoras (fingerprint, comportamiento) tienen impacto limitado.

### Arquitectura física

```
Servidor Ubuntu/Mac
├── USB → Teléfono 1 SIM A  ──► SOCKS5 :1080  → grupo A (cuentas 1-10)
├── USB → Teléfono 1 SIM B  ──► SOCKS5 :1081  → grupo B (cuentas 11-20)
├── USB → Teléfono 2        ──► SOCKS5 :1082  → grupo C (cuentas 21-35)
├── USB → Teléfono 3        ──► SOCKS5 :1083  → grupo D (cuentas 36-50)
└── USB → Teléfono 4        ──► SOCKS5 :1084  → grupo E (cuentas 51-60)
```

Cada teléfono expone un proxy SOCKS5 local vía USB tethering.
**App recomendada (Android):** Every Proxy (gratis) o Proxy Server (Majed Alharbi).

### Configuración física

1. Activar USB tethering en cada teléfono.
2. El servidor ve cada teléfono como interfaz de red (`usb0`, `usb1`, etc.).
3. Configurar la app proxy en cada teléfono para escuchar en puerto único (1080, 1081…).
4. Verificar: `curl --proxy socks5://192.168.x.1:1080 https://api.ipify.org` devuelve IP móvil.

### Solución técnica — Proxy Pool Manager

#### Tabla `proxy_nodes` en SQLite
```sql
CREATE TABLE IF NOT EXISTS proxy_nodes (
    id            TEXT PRIMARY KEY,        -- "phone1_simA", "phone2", etc.
    label         TEXT NOT NULL,           -- "Teléfono 1 SIM A"
    server        TEXT NOT NULL,           -- "socks5://192.168.42.1:1080"
    status        TEXT NOT NULL DEFAULT 'online',
                                           -- online | offline | low_data | maintenance
    last_checked  TEXT,
    last_seen_ip  TEXT,
    check_fail_count INTEGER NOT NULL DEFAULT 0,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS account_proxy_assignment (
    account_name   TEXT PRIMARY KEY REFERENCES accounts(name),
    primary_node   TEXT NOT NULL REFERENCES proxy_nodes(id),
    secondary_node TEXT REFERENCES proxy_nodes(id),  -- fallback
    assigned_at    TEXT NOT NULL
);
```

#### Módulo `proxy_manager.py`
```python
"""
proxy_manager.py — Gestión del pool de SIM hotspots con health checking y fallback.
"""
import time
import socket
import logging
import threading
import requests
import job_store

logger = logging.getLogger("proxy_manager")

CHECK_INTERVAL_S = 120       # chequear cada 2 minutos
FAIL_THRESHOLD   = 3         # 3 fallos consecutivos → offline
RECOVERY_TIMEOUT = 300       # esperar 5 min antes de reintentar un nodo caído

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Health checking
# ---------------------------------------------------------------------------
def _check_node(node: dict) -> tuple[bool, str]:
    """Verifica si el nodo proxy está activo. Retorna (ok, ip_publica)."""
    server = node["server"]
    try:
        proxies = {"http": server, "https": server}
        resp = requests.get(
            "https://api.ipify.org?format=json",
            proxies=proxies,
            timeout=10,
        )
        if resp.status_code == 200:
            ip = resp.json().get("ip", "")
            return True, ip
    except Exception as e:
        logger.debug("[ProxyCheck] %s falló: %s", node["id"], e)
    return False, ""


def run_health_checker():
    """Hilo daemon que verifica todos los nodos cada CHECK_INTERVAL_S segundos."""
    while True:
        nodes = job_store.list_proxy_nodes()
        for node in nodes:
            ok, ip = _check_node(node)
            with _lock:
                if ok:
                    job_store.update_proxy_node_status(
                        node["id"],
                        status="online",
                        last_ip=ip,
                        reset_fails=True,
                    )
                    if node["status"] != "online":
                        logger.info("[Proxy] Nodo %s recuperado — IP: %s",
                                    node["id"], ip)
                else:
                    fails = node["check_fail_count"] + 1
                    new_status = "offline" if fails >= FAIL_THRESHOLD else node["status"]
                    job_store.update_proxy_node_status(
                        node["id"],
                        status=new_status,
                        fail_count=fails,
                    )
                    if new_status == "offline" and node["status"] != "offline":
                        logger.warning("[Proxy] Nodo %s OFFLINE tras %d fallos",
                                       node["id"], fails)
                        _alert_node_down(node)
        time.sleep(CHECK_INTERVAL_S)


def _alert_node_down(node: dict):
    """Notifica al operador que un nodo cayó."""
    logger.critical(
        "[ALERTA] Proxy caído: %s (%s) — cuentas afectadas: %s",
        node["label"], node["server"],
        job_store.get_accounts_for_node(node["id"]),
    )
    # En Fase 3: enviar webhook a OpenClaw / Telegram


# ---------------------------------------------------------------------------
# Selección de proxy para una cuenta
# ---------------------------------------------------------------------------
def resolve_proxy(account_name: str) -> dict | None:
    """
    Retorna el proxy a usar para esta cuenta.

    Lógica de prioridad:
    1. Primary node si está online.
    2. Secondary node si primary está offline.
    3. Otro nodo online del pool si secondary también falla.
    4. None si no hay ningún nodo disponible → job se encola para después.
    """
    assignment = job_store.get_proxy_assignment(account_name)
    if not assignment:
        logger.warning("[Proxy] Sin asignación para cuenta %s", account_name)
        return None

    candidates = [
        assignment["primary_node"],
        assignment.get("secondary_node"),
    ]

    for node_id in candidates:
        if not node_id:
            continue
        node = job_store.get_proxy_node(node_id)
        if node and node["status"] == "online":
            logger.info("[Proxy] %s usando nodo %s (%s)",
                        account_name, node_id, node["last_seen_ip"])
            return {"server": node["server"]}

    # Último recurso: cualquier nodo online (distinto grupo, acepta riesgo temporal)
    fallback = job_store.get_any_online_proxy_node(
        exclude_nodes=candidates
    )
    if fallback:
        logger.warning(
            "[Proxy] %s usando fallback de emergencia: %s — "
            "cuentas de grupos distintos compartirán IP temporalmente",
            account_name, fallback["id"],
        )
        return {"server": fallback["server"]}

    logger.error("[Proxy] Sin nodos disponibles para %s — job encolado", account_name)
    return None
```

#### Integración en `facebook_poster.py`
```python
import proxy_manager

# En __init__ o al inicio de login():
proxy = proxy_manager.resolve_proxy(self.account.name)
if proxy is None:
    raise RuntimeError(f"Sin proxy disponible para {self.account.name} — reintentando más tarde")

# En _build_browser():
launch_kwargs["proxy"] = proxy
```

#### Integración en `api_server.py`
Si `resolve_proxy()` retorna `None`, **no lanzar el job** — marcarlo como `pending` con `retry_after = now + 15min` y devolver `202 Queued` en vez de `202 Accepted`.

---

### Los tres escenarios de operación

#### Escenario A — Teléfono desconectado (cable USB)
```
Evento: usb0 desaparece del SO → proxy socks5://192.168.42.1:1080 no responde.

Health checker (cada 2 min):
  1. _check_node(phone1_simA) → falla.
  2. fail_count: 1 → 2 → 3 → status = "offline".
  3. Alerta en log + webhook.

Próximo job para cuenta del grupo A:
  resolve_proxy() → primary offline → intenta secondary (ej. phone3).
  Si phone3 online → job corre con IP de phone3 (riesgo temporal aceptable).
  Si no hay fallback → job encolado, se reintenta cada 15 min.

Operador:
  Reconecta USB → health checker detecta recuperación → status = "online".
  Jobs encolados se procesan automáticamente.
```

#### Escenario B — Saldo/datos agotados
```
Evento: SIM sin datos → requests.get falla con timeout (no connection refused).

Misma secuencia que Escenario A, con una diferencia:
  - El fallo es intermitente al principio (paquetes parciales antes de agotarse).
  - check_fail_count puede tardar más en llegar a 3.

Mitigación adicional: monitorear data usage con `adb shell dumpsys netstats` si
los teléfonos son Android con ADB habilitado. Opcional para Fase 2.

Operador:
  Recargar SIM → teléfono recupera conectividad → health checker detecta → online.
```

#### Escenario C — Todos los teléfonos funcionales (escenario normal)
```
Todos los nodos online:
  - Cada cuenta usa su primary proxy siempre.
  - Los grupos de cuentas nunca comparten IP.
  - Health checker confirma IPs cada 2 minutos y las loguea.

Maximizar uso:
  - Cuentas del mismo grupo se turnan (secuencial), no corren simultáneas.
  - Un nodo puede servir a 10-12 cuentas secuenciales sin riesgo.
  - Si todas las cuentas de un nodo están activas (raro), el scheduler
    prioriza por last_published_at: la que publicó hace más tiempo va primero.

Rotación natural de IP:
  - Las IPs móviles cambian al reconectar el carrier (~cada 24h o al reiniciar).
  - Esto es BUENO — simula que el usuario cambia de red.
  - No intentar mantener la misma IP forzosamente.
```

---

### Asignación de cuentas a nodos

Regla crítica: **cuentas que publican en los mismos grupos de Facebook deben estar en nodos distintos**.

```python
def assign_proxy_to_account(account_name: str, account_groups: list[str]) -> str:
    """
    Asigna el nodo con menos cuentas que no comparta grupos con esta cuenta.
    Retorna el node_id asignado.
    """
    nodes = job_store.list_proxy_nodes()
    best_node = None
    best_score = float("inf")

    for node in nodes:
        existing_accounts = job_store.get_accounts_for_node(node["id"])
        existing_groups = set()
        for acc in existing_accounts:
            existing_groups.update(acc["groups"])

        overlap = len(set(account_groups) & existing_groups)
        count = len(existing_accounts)

        # Penalizar fuertemente el solapamiento de grupos
        score = overlap * 100 + count
        if score < best_score:
            best_score = score
            best_node = node["id"]

    return best_node
```

### Criterio de aceptación
- [ ] 5 nodos configurados en DB, cada uno con IP verificada distinta.
- [ ] `resolve_proxy()` retorna proxy correcto para cada cuenta.
- [ ] Desconectar teléfono → en < 6 min el nodo aparece como `offline` en `/admin`.
- [ ] Job para cuenta sin proxy disponible queda `pending` y se reintenta automáticamente.
- [ ] Reconectar teléfono → en < 4 min el nodo vuelve a `online` sin intervención manual.
- [ ] Cuentas que comparten grupos de FB nunca tienen el mismo primary proxy.
- [ ] Test: apagar 2 de 5 teléfonos → el sistema sigue publicando con los 3 restantes.

### Riesgos
- **Carrier cambia el rango de IP:** poco frecuente pero posible. El health checker detecta el cambio y actualiza `last_seen_ip`. No requiere intervención.
- **Teléfono se queda sin batería:** igual que desconexión. Recomendación: conectar teléfonos a cargadores permanentemente.
- **SIM dual: cambio automático entre SIMs:** configurar en Android que los datos usen SIM A o SIM B específicamente, no "automático". Así cada app proxy siempre sale por la SIM asignada.

---

## 1.2 — Password individual cifrada

### Problema
`FB_PASSWORD` es una variable única en `.env` usada por todas las cuentas ([config.py:117](../facebook_auto_poster/config.py#L117)). Si una cuenta se compromete, todas caen. Además, Facebook detecta "mismo password hash" a través de eventos de login agrupados (no directamente, pero por tiempos y patrones de uso).

### Solución técnica

**1. Instalar dependencia:**
```
cryptography>=42.0.0
```

**2. Crear `crypto.py`:**
```python
from cryptography.fernet import Fernet
from pathlib import Path
import os

KEY_PATH = Path(__file__).parent / ".secret.key"

def _get_or_create_key() -> bytes:
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes()
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    os.chmod(KEY_PATH, 0o600)
    return key

_fernet = Fernet(_get_or_create_key())

def encrypt(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()

def decrypt(enc: str) -> str:
    return _fernet.decrypt(enc.encode()).decode()
```

`.secret.key` se añade a `.gitignore`. En producción la clave vive en un secret manager (AWS Secrets Manager, Doppler, Vault).

**3. Migrar tabla `accounts`:**
```sql
ALTER TABLE accounts ADD COLUMN password_enc TEXT;
```

**4. Flujo:**
- Admin panel: al crear/editar cuenta, recibe password en claro vía POST (HTTPS obligatorio), cifra, guarda.
- `load_accounts()` descifra al construir `AccountConfig`.
- `.env` ya no necesita `FB_PASSWORD`. Fallback opcional para backwards compat.

**5. Endpoint admin:**
```python
@app.post("/admin/api/accounts/<name>/password")
@admin_required
def admin_set_password(name: str):
    data = request.get_json()
    new_pw = data.get("password", "").strip()
    if len(new_pw) < 6:
        return jsonify({"error": "Password muy corto"}), 400
    job_store.set_account_password(name, crypto.encrypt(new_pw))
    return "", 204
```

### Criterio de aceptación
- [ ] `jobs.db` no contiene passwords en claro (verificar con `sqlite3 jobs.db "SELECT password_enc FROM accounts"`).
- [ ] Rotar el password de una cuenta desde UI admin funciona sin reiniciar el server.
- [ ] Borrar `.secret.key` → el servidor rechaza arrancar con error claro.

### Riesgos
- **Pérdida de `.secret.key`:** backups obligatorios. Si se pierde, todas las passwords son irrecuperables y hay que resetear cada cuenta.
- **Migración:** escribir script `migrate_passwords.py` que tome la password global de `.env` y la cifre por cuenta.

---

## 1.3 — Fingerprint variation por cuenta

### Problema
[facebook_poster.py:179-195](../facebook_auto_poster/facebook_poster.py#L179-L195): mismo UA, mismo viewport (1280×720), misma locale `es-ES` para todas. Aunque Patchright oculta `navigator.webdriver`, **el hash de canvas/WebGL/audio/fonts/hardware-concurrency es idéntico** entre cuentas → Facebook correlaciona.

### Solución técnica

**1. Catálogo de fingerprints realistas** (guardar en `fingerprints.json`):
```json
[
  {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "viewport": [1366, 768],
    "locale": "es-MX",
    "timezone": "America/Mexico_City",
    "platform": "Win32",
    "hardware_concurrency": 8,
    "device_memory": 8,
    "color_scheme": "light",
    "sec_ch_ua": "\"Google Chrome\";v=\"132\", \"Chromium\";v=\"132\", \"Not?A_Brand\";v=\"24\"",
    "sec_ch_ua_platform": "\"Windows\""
  },
  {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "viewport": [1440, 900],
    "locale": "es-AR",
    "timezone": "America/Argentina/Buenos_Aires",
    ...
  }
]
```

**2. Asignar fingerprint a cada cuenta al crearla** (guardado en DB):
```sql
ALTER TABLE accounts ADD COLUMN fingerprint_json TEXT;
```

**3. Aplicar al construir context** ([facebook_poster.py:191](../facebook_auto_poster/facebook_poster.py#L191)):
```python
fp = self.account.fingerprint  # dict
context = browser.new_context(
    user_agent=fp["user_agent"],
    viewport={"width": fp["viewport"][0], "height": fp["viewport"][1]},
    locale=fp["locale"],
    timezone_id=fp["timezone"],
    color_scheme=fp["color_scheme"],
    extra_http_headers={
        "sec-ch-ua": fp["sec_ch_ua"],
        "sec-ch-ua-platform": fp["sec_ch_ua_platform"],
    },
)
# Override navigator props que Patchright no randomiza
context.add_init_script(f"""
Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {fp['hardware_concurrency']}}});
Object.defineProperty(navigator, 'deviceMemory', {{get: () => {fp['device_memory']}}});
""")
```

**4. Canvas/WebGL noise:**
Patchright base es bueno pero no randomiza cada instance. Opciones:
- Usar **`patchright-stealth-scripts`** (si existe fork mantenido).
- Alternativa más robusta: migrar a **[Camoufox](https://github.com/daijro/camoufox)** que tiene anti-fingerprinting activo.
- Script manual de canvas noise (inyectar ruido aleatorio en `HTMLCanvasElement.prototype.toDataURL`).

**5. Variar `browser_window_position` por cuenta** para no superponer en modo parallel:
```python
pos = (random.randint(0, 200), random.randint(0, 100))
```

### Criterio de aceptación
- [ ] Cada cuenta tiene un fingerprint persistente guardado en DB (no cambia entre sesiones).
- [ ] Test con https://bot.sannysoft.com/ o https://pixelscan.net/: cada cuenta da score diferente, todas pasan "no bot detected".
- [ ] Test con https://amiunique.org/: cada cuenta es "único" con distinta firma.
- [ ] El mismo fingerprint persiste al reabrir el browser (no cambia en cada `_build_browser()`).

### Riesgos
- **Inconsistencia UA ↔ client hints:** si `sec-ch-ua` dice Chrome 132 pero UA dice 124 → señal roja. Generar ambos del mismo template.
- **Timezone vs IP:** si el proxy es MX pero timezone dice AR → señal roja. Alinear ambos.

---

## 1.4 — Ventana horaria realista + timezone por cuenta

### Problema
[config.py:50](../facebook_auto_poster/config.py#L50): `"post_hours_allowed": range(0, 24)` con comentario `TODO: revert to range(6, 23) after testing`. Publicar a las 3 AM hora local es **bandera roja masiva**.

Además, la validación actual usa `datetime.now().hour` (hora del servidor), no la hora local de cada cuenta. Si el servidor corre en UTC y la cuenta es MX, la "hora válida" no coincide con comportamiento natural.

### Solución técnica

**1. Revertir default:**
```python
"post_hours_allowed": range(7, 23),  # 7 AM - 22:59 local
```

**2. Agregar variación por cuenta** (cada cuenta tiene su "prime time"):
```python
@dataclass
class AccountConfig:
    ...
    active_hours: tuple[int, int] = (7, 23)  # local
    timezone: str = "America/Mexico_City"
```

Algunas cuentas pueden ser "tempraneras" (6-20), otras "nocturnas" (10-23:30). Asignar aleatoriamente al crear cuenta y persistir.

**3. Chequeo usando timezone de la cuenta** ([account_manager.py:140](../facebook_auto_poster/account_manager.py#L140)):
```python
from zoneinfo import ZoneInfo

def _is_hour_allowed(account: AccountConfig) -> bool:
    tz = ZoneInfo(account.timezone)
    local_hour = datetime.now(tz).hour
    start, end = account.active_hours
    return start <= local_hour < end
```

**4. En `api_server.py`:** `_hour_allowed` debe evaluar **por cuenta** filtrada. Si una cuenta está fuera de horario, se salta pero otras continúan. Ya no es un 403 global.

### Criterio de aceptación
- [ ] `config.py` muestra `range(7, 23)` sin TODO.
- [ ] Cada cuenta tiene `active_hours` + `timezone` en DB.
- [ ] Intentar publicar a una cuenta fuera de su horario local devuelve 403 solo para esa cuenta, no bloquea el job global.
- [ ] Los logs muestran la hora local evaluada, no la del servidor.

### Riesgos
- Si el servidor cambia de host geográfico, `datetime.now()` sin tz podría fallar. Usar siempre `datetime.now(ZoneInfo(...))`.

---

## 1.5 — Typo rate realista + patrón humano

### Problema
[facebook_poster.py:247](../facebook_auto_poster/facebook_poster.py#L247) — 5% de chance de typo por carácter, con corrección inmediata char-por-char. Humanos reales:
- Typo rate: 1-2% en desktop con teclado.
- Corrección: al notar el error, borran 1-3 caracteres de golpe, no char por char.
- A veces **no corrigen** errores pequeños.

Ese patrón actual es detectable por análisis de timings entre `keydown` events.

### Solución técnica

**Reescribir `_human_type`:**

```python
def _human_type(self, locator, text: str) -> None:
    try:
        locator.click(timeout=5000)
    except Exception:
        try: locator.focus(timeout=3000)
        except Exception: pass

    kb = self.page.keyboard
    i = 0
    while i < len(text):
        char = text[i]

        # Typo con corrección agrupada (1.5% chance, no en espacios)
        if char != " " and random.random() < 0.015:
            # Escribir 1-3 chars incorrectos antes de corregir
            n_wrong = random.choices([1, 2, 3], weights=[70, 25, 5])[0]
            wrong_chars = random.choices(self._TYPO_ALPHABET, k=n_wrong)
            for wc in wrong_chars:
                kb.type(wc)
                time.sleep(random.uniform(0.06, 0.14))
            # Pausa de "darse cuenta"
            time.sleep(random.uniform(0.25, 0.60))
            # Borrar de golpe (simula ctrl+backspace o varios backspaces rápidos)
            for _ in range(n_wrong):
                kb.press("Backspace")
                time.sleep(random.uniform(0.03, 0.07))
            time.sleep(random.uniform(0.10, 0.25))

        kb.type(char)

        # Delay inter-char con distribución log-normal (más realista que uniform)
        if char == " ":
            time.sleep(random.lognormvariate(-2.0, 0.4))  # ~0.13s mediana
        elif char in ".,;:!?":
            time.sleep(random.lognormvariate(-1.7, 0.4))  # pausa post-puntuación
        else:
            time.sleep(random.lognormvariate(-2.7, 0.35))  # ~0.07s mediana

        # Micro-pausa ocasional (2%, no 5%)
        if random.random() < 0.02:
            time.sleep(random.uniform(0.40, 1.10))

        i += 1
```

**Métricas humanas de referencia:**
- WPM promedio adulto: 35-45 WPM = 175-225 CPM = ~0.27-0.35s entre chars.
- Pausas entre palabras (espacio): 0.15-0.25s.
- Pausas post-puntuación: 0.25-0.40s.
- Typo rate: 1-2%.
- Distribución de delays: log-normal, no uniform (los humanos tienen "ráfagas" rápidas y pausas de pensamiento).

**Retirar "palabras fantasma":**
El bloque `_FAKE_WORDS` ([facebook_poster.py:230-244](../facebook_auto_poster/facebook_poster.py#L230-L244)) escribe "aaa/zzz/hmm/err" y las borra. Es una señal artificial — **remover**. Los humanos no escriben "zzz" como typo.

### Criterio de aceptación
- [ ] Tipear 100 caracteres tarda 25-40 segundos (~150-240 CPM).
- [ ] Log de tiempos entre keydown muestra distribución aproximadamente log-normal.
- [ ] Ratio de backspaces / chars totales ≈ 1-2%.
- [ ] Test manual: grabar video, comparar con persona real tecleando.

### Riesgos
- No hay riesgo técnico. Solo calibración.

---

## 1.6 — Migración cross-platform Windows → Ubuntu/Mac

### Problema
El proyecto tiene binarios Windows hardcodeados que bloquean ejecución en Ubuntu y Mac:
- `chromedriver.exe` — en la raíz del proyecto.
- `cloudflared.exe` — referenciado en [main.py:47](../facebook_auto_poster/main.py#L47).
- `CHROME_PROFILE_PATH` en `.env.example` apunta a ruta Windows.
- `browser_window_position` y Emunium usan coordenadas de pantalla que varían por OS.

### Solución técnica

**1. Eliminar `chromedriver.exe`:**
Patchright descarga y gestiona su propio Chromium con `patchright install chromium`. El `chromedriver.exe` en la raíz no se usa — eliminar y añadir al `.gitignore`.

**2. Cloudflared multiplataforma** ([main.py:47](../facebook_auto_poster/main.py#L47)):
```python
import platform
import shutil

def _find_cloudflared() -> str | None:
    """Localiza el binario cloudflared según el OS."""
    system = platform.system()

    # 1. Buscar en PATH primero (instalado via brew, apt, etc.)
    which = shutil.which("cloudflared")
    if which:
        return which

    # 2. Fallback: binario junto al proyecto
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    candidates = {
        "Windows": PROJECT_ROOT / "cloudflared.exe",
        "Linux":   PROJECT_ROOT / "cloudflared",
        "Darwin":  PROJECT_ROOT / "cloudflared",
    }
    candidate = candidates.get(system)
    if candidate and candidate.exists():
        return str(candidate)

    return None


def start_cloudflared(port: int) -> None:
    exe = _find_cloudflared()
    if not exe:
        main_logger.warning(
            "cloudflared no encontrado. Instalar con: "
            "brew install cloudflared  (Mac) | "
            "sudo apt install cloudflared  (Ubuntu)"
        )
        return
    # ... resto igual
```

**Instalación recomendada:**
```bash
# Ubuntu
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared

# Mac
brew install cloudflared
```

**3. Chrome profile path multiplataforma** (`.env.example`):
```ini
# Dejar vacío para que Patchright use perfil temporal (recomendado con user_data_dir de Fase 2.1)
# O especificar ruta según OS:
# Mac:   /Users/TU_USUARIO/Library/Application Support/Google/Chrome/Default
# Ubuntu: /home/TU_USUARIO/.config/google-chrome/Default
CHROME_PROFILE_PATH=
```

**4. Paths en código:** reemplazar cualquier `\\` o `os.sep` hardcodeado por `Path()`. Revisar especialmente `account.log_file` y `account.screenshots_dir` en [config.py:101-106](../facebook_auto_poster/config.py#L101-L106) — ya usan `Path`, OK.

**5. Emunium en Ubuntu/Mac:**
Emunium usa `pyautogui` internamente, que en Linux requiere `python3-xlib` y en Mac no tiene restricciones. Añadir a docs de setup:
```bash
# Ubuntu (X11 o XWayland)
sudo apt install python3-xlib scrot

# Si corre headless (sin display físico):
sudo apt install xvfb
Xvfb :99 -screen 0 1280x720x24 &
export DISPLAY=:99
```

**6. Script de setup unificado (`setup.sh`):**
```bash
#!/usr/bin/env bash
set -euo pipefail

OS=$(uname -s)
echo "Detectado OS: $OS"

# Instalar dependencias Python
pip install -r requirements.txt
patchright install chromium

# Dependencias según OS
if [ "$OS" = "Linux" ]; then
    sudo apt-get install -y python3-xlib scrot
    # Instalar cloudflared
    curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
      -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared
elif [ "$OS" = "Darwin" ]; then
    brew install cloudflared || true
fi

echo "Setup completo. Copiar .env.example a .env y ejecutar: python main.py"
```

### Criterio de aceptación
- [ ] `python main.py` arranca sin errores en Ubuntu fresh install.
- [ ] `python main.py` arranca sin errores en Mac (Intel y Apple Silicon).
- [ ] `cloudflared.exe` eliminado del repo, `.gitignore` actualizado.
- [ ] `chromedriver.exe` eliminado del repo.
- [ ] `setup.sh` ejecutable cubre todo el setup en un solo comando.
- [ ] Logs no contienen rutas con `\` (Windows separator).

### Riesgos
- **Emunium en headless Linux:** sin display físico, los clicks a nivel OS no tienen destino. Solución: en servidores sin pantalla, deshabilitar Emunium (`emunium_enabled: false`) y confiar en los clicks de Patchright. El anti-detección de Patchright es suficiente en headless.
- **Apple Silicon (M1/M2/M3):** Patchright descarga Chromium ARM. Verificar que `patchright install chromium` funciona en arm64. Si no, usar Rosetta: `arch -x86_64 pip install patchright`.

---

## Orden de implementación sugerido

```
Día 1:    1.6 (Cross-platform)  ← primero: necesario para trabajar en Ubuntu/Mac
Día 2-3:  1.1 (SIM hotspots)   ← dependencia fuerte: sin esto, los otros no importan
            └─ Setup físico de teléfonos (puede hacerse en paralelo con 1.6)
Día 4:    1.2 (Password cifrada)
Día 5-6:  1.3 (Fingerprint)
Día 6:    1.4 (Horario)         ← pequeño, hacer junto con 1.3
Día 6:    1.5 (Typing)          ← aislado, puede hacerse en cualquier momento
```

## Métricas de validación de fin de Fase 1

Después de 7 días de operación con los cambios:
- **Tasa de login exitoso:** > 95%.
- **Soft-bans detectados:** 0.
- **CAPTCHAs:** < 1 por cada 50 logins.
- **Fingerprint score (amiunique.org):** cada cuenta con hash único.
- **IPs verificadas:** 5 distintas, todas móviles, cada una en grupo de cuentas separado.
- **Resiliencia:** 1 teléfono desconectado → sistema continúa operando con los otros 4 en < 6 min automáticamente.
- **Cross-platform:** `python main.py` arranca limpio en Ubuntu y Mac sin modificaciones.
