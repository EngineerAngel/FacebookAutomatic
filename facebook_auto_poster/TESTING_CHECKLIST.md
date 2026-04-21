# Testing Checklist — Patchright + Emunium Migration

## Sanity Checks (Pre-Facebook)

- [x] Imports sin errores: `python -c "from facebook_poster import FacebookPoster"`
- [x] CONFIG keys presentes: `emunium_enabled` y `browser_window_position`
- [x] `main.py` arranca sin `NotImplementedError` (event loop policy OK)
- [x] Browser abre visible en (0,0) al instanciar `FacebookPoster`
- [x] `poster.close()` no deja procesos `chrome.exe` zombies

## Setup Interactive (setup_accounts.py)

- [x] Corre sin imports de Selenium
- [x] Abre login page sin errores
- [x] Intenta restaurar cookies si existen (confirmado: carmen restauró 7 cookies)
- [x] Espera ENTER del usuario después del login manual
- [x] Guarda cookies en DB después de confirmar sesión

## Login Flow

- [x] Login con cookies existentes (sin pasar por formulario)
- [x] Login con email/password (carmen: login + 2FA exitosos)
- [ ] Typos falsos (5%) + palabras fantasma (5%) no corrompen credenciales

## Publicación Básica

- [x] Publicar texto en 1 grupo (zarai, 2 tests exitosos)
- [x] Publicar texto + imagen en 1 grupo (zarai, imagen enviada con `set_input_files`)
- [ ] Thumbnail aparece después de `set_input_files` (warning: timeout — ver Bug #4)
- [x] Publicación exitosa sin errores

## Human Simulation

- [ ] Log muestra `[Idle]` ~20% de probabilidad entre grupos
- [x] Clicks se ejecutan vía Emunium (log: `[Emunium] Activo offset=0,85`)
- [ ] Typing se ejecuta con delays entre caracteres (no instantáneo)
- [x] Text variation con zero-width chars se aplica (log: `Text variation applied`)

## Detection & Evasion

- [ ] **CAPTCHA**: aparece → `_wait_for_manual_resolution` se activa
- [ ] **Soft-ban**: "temporarily blocked" → `_detect_challenge()` retorna `"banned"`, `_banned=True`
- [ ] **Checkpoint**: detectado como checkpoint → retries abortan
- [x] Screenshots guardados en `screenshots/{account}/` en errores (confirmado)

## Session Refresh

- [ ] `refresh_every_n_posts=2` → aparece `[Refresh]` en log tras grupos #2, #4
- [ ] Navegación a home funciona entre refresh
- [ ] Cookies se mantienen tras refresh

## Multi-Account

### Secuencial (`EXECUTION_MODE=sequential`)
- [ ] 2 cuentas, 1 grupo c/u → ambas completan OK
- [ ] Delays `wait_between_accounts_min/max` respetados
- [ ] Logs separados por cuenta

### Paralelo (`EXECUTION_MODE=parallel`)
- [ ] 2 cuentas simultáneas lanzan 2 Patchright
- [ ] Ambas completan sin cross-contamination
- [ ] Startup lento (aceptable vs Selenium)

## Regression Detection

- [x] `job_store.save/load_cookies` funciona con formato Playwright
- [x] `record_login` registra suceso en BD
- [ ] API `/post` y `/schedule` funcionan end-to-end
- [x] Logs contienen prefijos: `[Login]`, `[Cookies]`, `[Driver]`, `[Publish]`
- [x] `page.screenshot()` captura errores correctamente

## Configuration

- [x] `browser_window_position = (0, 0)` sincroniza Emunium
- [x] `emunium_enabled = True` activo
- [x] `post_hours_allowed = range(0, 24)` para testing
- [ ] Fallback a Patchright puro cuando `emunium_enabled = False`

## Edge Cases

- [ ] Grupo inválido → error graceful, continúa con siguiente
- [ ] Imagen corrupta → error capturado, continúa
- [ ] Red lenta → retries funcionan sin crash
- [ ] URL en texto → `_dismiss_link_preview` elimina card antes de publicar

---

## Bugs encontrados y corregidos

| # | Descripción | Estado |
|---|---|---|
| 1 | Publicación se hacía en campo de comentarios (selector muy genérico) | Corregido `0e98b89` |
| 2 | Loop infinito post-publicación (esperaba dialog genérico) | Corregido `0e98b89` |
| 3 | Event loop policy innecesaria en Python 3.13 Windows | Corregido `82162c0` |
| 4 | Thumbnail selector con backslashes en lugar de `//` — timeout pero publish OK | Investigando |

## Notas

- **Cuenta carmen:** soft-ban activo (posts desaparecen al refrescar). Usar otra cuenta para continuar testing.
- **Critical:** Revert `post_hours_allowed` a `range(6, 23)` antes de producción.
- **Headless mode:** `browser_headless = True` + `emunium_enabled = False` para CI/Docker.
- **Multiprocess:** Cada proceso lanza su propio Patchright — startup más lento que Selenium.
