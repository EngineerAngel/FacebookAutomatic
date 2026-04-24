# Avance — Fase 1: Stop-the-bleeding

> Última actualización: 2026-04-23

## Estado general

| # | Ítem | Estado | Completado |
|---|------|--------|-----------|
| 1.1 | SIM hotspot pool con resiliencia | ⏳ Pendiente | — |
| 1.2 | Password individual cifrada | ⏳ Pendiente | — |
| 1.3 | Fingerprint variation (UA + viewport + locale + TZ) | ✅ Completado | 2026-04-23 |
| 1.4 | Ventana horaria realista + timezone por cuenta | ✅ Completado | 2026-04-23 |
| 1.5 | Bajar typo rate y mejorar patrón de corrección | ✅ Completado | 2026-04-23 |
| 1.6 | Migración cross-platform Windows → Ubuntu/Mac | ⏳ Pendiente | — |

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

### ⏳ 1.2 — Password individual cifrada

**Pendiente:**
- [ ] Crear `crypto.py` con Fernet
- [ ] Migración: `ALTER TABLE accounts ADD COLUMN password_enc TEXT`
- [ ] Script `migrate_passwords.py` (toma `FB_PASSWORD` global → cifra por cuenta)
- [ ] Endpoint `POST /admin/api/accounts/<name>/password`
- [ ] `load_accounts()` descifra al construir `AccountConfig`
- [ ] Añadir `.secret.key` a `.gitignore`

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

### ⏳ 1.6 — Migración cross-platform Windows → Ubuntu/Mac

**Pendiente:**
- [ ] Función `_find_cloudflared()` multiplataforma en `main.py`
- [ ] Eliminar `chromedriver.exe` del repo (añadir a `.gitignore`)
- [ ] Actualizar `.env.example` con `CHROME_PROFILE_PATH=` vacío
- [ ] Crear `setup.sh` unificado para Ubuntu/Mac
- [ ] Verificar startup limpio en Ubuntu y Mac

---

## Métricas de validación de Fase 1 (al completar los 6 ítems)

| Métrica | Target | Estado |
|---------|--------|--------|
| Tasa de login exitoso | > 95% | Sin datos |
| Soft-bans detectados | 0 | Sin datos |
| CAPTCHAs | < 1 / 50 logins | Sin datos |
| Fingerprint único por cuenta (amiunique.org) | ✓ | Pendiente 1.3 |
| IPs distintas verificadas | 5 móviles | Pendiente 1.1 |
| Startup limpio en Ubuntu/Mac | ✓ | Pendiente 1.6 |
