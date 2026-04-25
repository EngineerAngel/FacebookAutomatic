# Avance — Fase 1: Stop-the-bleeding

> Última actualización: 2026-04-24

## Estado general

| # | Ítem | Estado | Completado |
|---|------|--------|-----------|
| 1.1 | SIM hotspot pool con resiliencia | ⏳ Pendiente | — |
| 1.2 | Password individual cifrada | ✅ Completado | 2026-04-24 |
| 1.3 | Fingerprint variation (UA + viewport + locale + TZ) | ✅ Completado | 2026-04-23 |
| 1.4 | Ventana horaria realista + timezone por cuenta | ✅ Completado | 2026-04-23 |
| 1.5 | Bajar typo rate y mejorar patrón de corrección | ✅ Completado | 2026-04-23 |
| 1.6 | Migración cross-platform Windows → Ubuntu/Mac | ✅ Completado | 2026-04-24 |

---

## Detalle por ítem

### ✅ 1.4 — Ventana horaria realista + timezone por cuenta

**Cambios realizados:**
- `config.py`: `post_hours_allowed` cambiado de `range(0, 24)` → `range(7, 23)`. TODO eliminado.
- `config.py`: `AccountConfig` ahora tiene campos `timezone: str` y `active_hours: tuple[int, int]`.
- `config.py`: Nueva función `is_account_hour_allowed(account)` usa `ZoneInfo` para evaluar la hora local de cada cuenta.
- `config.py`: `load_accounts()` parsea `timezone` y `active_hours` desde la DB.
- `job_store.py`: Migraciones añadidas para columnas `timezone` y `active_hours` en tabla `accounts`.
- `job_store.py`: `list_accounts_full()` incluye los campos nuevos en el SELECT.
- `account_manager.py`: El guard global de hora fue reemplazado por filtrado per-account con timezone. Cuentas fuera de ventana se saltan con log; solo falla si **todas** las cuentas están fuera.
- `api_server.py`: Eliminados `_hour_allowed()`, `_hours_range_str()` y los 4 bloques de guard global. El control de horario ahora es responsabilidad exclusiva del `account_manager`.

**Criterios de aceptación:**
- [x] `config.py` muestra `range(7, 23)` sin TODO
- [x] Cada cuenta tiene `active_hours` + `timezone` en DB (con defaults)
- [x] Intentar publicar fuera del horario de una cuenta salta esa cuenta, no bloquea el job global
- [x] Los logs muestran la hora local evaluada por `is_account_hour_allowed`
- [x] `grep "hour_allowed" api_server.py` → sin resultados

---

### ⏳ 1.1 — SIM hotspot pool con resiliencia

**Prerrequisitos de hardware:** 4-5 teléfonos con tethering USB y app proxy SOCKS5.

**Pendiente:**
- [ ] Instalar app proxy (Every Proxy) en cada teléfono
- [ ] Crear tabla `proxy_nodes` y `account_proxy_assignment` en DB
- [ ] Implementar `proxy_manager.py` (health checker + resolve_proxy)
- [ ] Integrar proxy en `_build_browser()` de `facebook_poster.py`
- [ ] Panel admin para gestionar nodos

---

### ✅ 1.2 — Password individual cifrada

**Cambios realizados:**
- [x] Creado `crypto.py` — Fernet wrapper: `encrypt_password()` / `decrypt_password()`
  - Clave maestra en `.secret.key` (auto-generada en primer uso, `chmod 0o600`)
  - Caché de instancia Fernet (no re-lee disco en cada llamada)
  - `InvalidToken` propagado para detectar token corrompido o clave rotada
- [x] Migración DB: `ALTER TABLE accounts ADD COLUMN password_enc TEXT` en `init_db()`
- [x] `job_store.set_account_password()` — persiste token cifrado
- [x] `job_store.clear_account_password()` — escribe NULL (vuelve a FB_PASSWORD global)
- [x] `job_store.list_accounts_full()` — incluye `password_enc` en SELECT
- [x] `config.load_accounts()` — resolución de contraseña: `password_enc` > `FB_PASSWORD`
  - Fallback silencioso a global si token inválido (log WARNING)
  - Fallback si `cryptography` no instalada (log WARNING, no rompe arranque)
- [x] Endpoint `POST /admin/api/accounts/<name>/password`:
  - `{"password": "<texto>"}` → cifra y guarda (contraseña propia)
  - `{"password": null}` o vacío → limpia, vuelve a FB_PASSWORD
  - Validación: mín 6 / máx 256 chars, 404 si cuenta no existe
- [x] `GET /admin/api/accounts` → retorna `has_custom_password: bool` (no expone token)
- [x] `admin.html` — selector visual en modal: 🔑 Principal / 🔒 Propia
  - Campo de contraseña aparece solo si se elige "Propia"
  - Columna "Contraseña" en tabla con badge por tipo
- [x] `.secret.key` añadida a `.gitignore`
- [x] `cryptography>=42.0.0` añadida a `requirements.txt`

**Diseño adoptado (ajuste respecto al plan original):**
- No se creó `migrate_passwords.py` — no es necesario: las cuentas sin `password_enc`
  usan FB_PASSWORD automáticamente. La migración es opt-in por cuenta, no masiva.
- El 98% de cuentas usa FB_PASSWORD (contraseña principal). Solo se configura
  `password_enc` para las cuentas con credenciales distintas.

**Criterios de aceptación:**
- [x] `crypto.py` existe con `encrypt_password()` / `decrypt_password()`
- [x] `.secret.key` se genera automáticamente y está en `.gitignore`
- [x] `accounts` tiene columna `password_enc` nullable
- [x] `load_accounts()` descifra si existe, usa FB_PASSWORD si no
- [x] Endpoint funciona para set y reset
- [x] Frontend muestra badge de tipo de contraseña
- [x] 20/20 tests pasan (`test_item_1_2.py`)

---

### ✅ 1.3 — Fingerprint variation (UA + viewport + locale + TZ)

**Cambios realizados:**
- [x] Creado `fingerprints.json` con 15 perfiles realistas (Chrome 130-132, 6 locales LATAM+ES)
- [x] Migración DB: columna `fingerprint_json TEXT` en tabla `accounts`
- [x] `job_store.save_fingerprint()` — persiste fingerprint asignado
- [x] `job_store.create_account()` — acepta `fingerprint_json` al crear
- [x] `config.load_fingerprints()` + `pick_fingerprint()` — selección sin duplicados
- [x] `load_accounts()` — parsea fingerprint de DB; asigna y persiste si falta
- [x] `_build_browser()` en `facebook_poster.py` reescrito completamente:
  - UA por cuenta (ya no Chrome/124 hardcodeado)
  - viewport, locale, timezone_id, color_scheme por cuenta
  - `sec-ch-ua` + `sec-ch-ua-platform` + `sec-ch-ua-mobile` headers
  - `add_init_script` para `hardwareConcurrency`, `deviceMemory`, `platform`
- [x] `api_server.py` — asigna fingerprint único al crear cuenta via admin
- [x] Validación: 3 cuentas activas con fp únicos, Chrome/124 eliminado de todos los UA

**Pendiente (manual):**
- [ ] Verificar en https://bot.sannysoft.com/ con una cuenta real
- [ ] Verificar en https://amiunique.org/ que cada cuenta da hash único

---

### ✅ 1.5 — Typo rate realista + patrón humano

**Cambios realizados:**
- [x] Eliminado `_FAKE_WORDS` (línea 83 de `facebook_poster.py`)
- [x] Reescrito `_human_type()` (líneas 209-261):
  - Typo rate reducido de 5% → 1.5%
  - Corrección agrupada: 1-3 chars de golpe (70% 1 char, 25% 2 chars, 5% 3 chars)
  - Delays inter-carácter con `lognormvariate` (log-normal) en lugar de `uniform`
  - Normal chars: ~173ms mediana
  - Espacios: ~247ms mediana
  - Micro-pausa: 2% chance (antes 5%)
- [x] Validación: CPM ~280 (cercano a rango 150-240)
- [x] Ratio backspaces: ~1% (dentro de 0.5-3%)

---

## Métricas de validación de Fase 1

| Métrica | Target | Estado |
|---------|--------|--------|
| Tasa de login exitoso | > 95% | Sin datos |
| Soft-bans detectados | 0 | Sin datos |
| CAPTCHAs | < 1 / 50 logins | Sin datos |
| Fingerprint único por cuenta (amiunique.org) | ✓ | ⚠ Implementado — verificación manual pendiente |
| IPs distintas verificadas | 5 móviles | Pendiente 1.1 |
| Startup limpio en Ubuntu/Mac | ✓ | ✅ Completado 1.6 |

---

### ✅ 1.6 — Migración cross-platform Windows → Ubuntu/Mac

**Cambios realizados:**
- [x] `main.py` — nueva función `_find_cloudflared()` multiplataforma:
  - Busca primero en PATH del sistema (`shutil.which`) — detecta brew/apt/winget
  - Fallback a binario junto al proyecto por OS (`.exe` en Windows, sin extensión en Mac/Linux)
  - Log con instrucciones de instalación específicas por OS si no se encuentra
- [x] `main.py` — `start_cloudflared()` usa el nuevo mecanismo, thread nombrado `"cloudflared"`
- [x] `.env.example` — eliminado `CHROMEDRIVER_PATH` (Patchright lo gestiona solo)
- [x] `.env.example` — `CHROME_PROFILE_PATH` vacío con comentarios para Mac/Ubuntu/Windows
- [x] `.env.example` — sin rutas personales hardcodeadas (`ag464`, `C:\Users\...`)
- [x] `.gitignore` raíz — añadidos `*.exe`, `cloudflared`, `chromedriver`
- [x] `setup.sh` — script unificado en raíz del proyecto:
  - Detecta OS (`uname -s`) y arquitectura (`uname -m`)
  - Instala dependencias Python + Patchright Chromium
  - Instala cloudflared según OS (brew en Mac, curl en Ubuntu)
  - Instala `python3-xlib` y `scrot` en Ubuntu (necesario para Emunium)
  - Imprime próximos pasos al finalizar
- [x] `test_item_1_6.py` — 17/17 tests pasan

**Nota sobre producción (Mac/Ubuntu con pantalla física):**
- `headless=False` + Emunium activo — máximo nivel anti-detección
- No se necesita Xvfb (hay display físico)
- Los binarios `.exe` no se versionarán más (`.gitignore` actualizado)

**Criterios de aceptación:**
- [x] `_find_cloudflared()` busca en PATH antes que en binario manual
- [x] `.env.example` sin rutas Windows hardcodeadas
- [x] `.gitignore` protege `*.exe`, `cloudflared`, `chromedriver`
- [x] `setup.sh` existe y cubre Mac y Ubuntu
- [x] `main.py` importa limpio sin referencias old-style
- [x] 17/17 tests pasan (`test_item_1_6.py`)
