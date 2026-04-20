# Testing Checklist — Patchright + Emunium Migration

## Sanity Checks (Pre-Facebook)

- [ ] Imports sin errores: `python -c "from facebook_poster import FacebookPoster"`
- [ ] CONFIG keys presentes: `emunium_enabled` y `browser_window_position`
- [ ] `main.py` arranca sin `NotImplementedError` (event loop policy OK)
- [ ] Browser abre visible en (0,0) al instanciar `FacebookPoster`
- [ ] `poster.close()` no deja procesos `chrome.exe` zombies

## Setup Interactive (setup_accounts.py)

- [ ] Corre sin imports de Selenium: `python setup_accounts.py cuenta_test`
- [ ] Abre login page sin errores
- [ ] Intenta restaurar cookies si existen
- [ ] Espera ENTER del usuario después del login manual
- [ ] Guarda cookies en DB después de confirmar sesión

## Login Flow

- [ ] Login con cookies existentes (sin pasar por formulario)
- [ ] Login con email/password (cookies borradas primero)
- [ ] Typos falsos (5%) + palabras fantasma (5%) no corrompen credenciales

## Publicación Básica

- [ ] Publicar texto en 1 grupo
- [ ] Publicar texto + imagen en 1 grupo
- [ ] Thumbnail aparece después de `set_input_files`
- [ ] Publicación exitosa sin errores

## Human Simulation

- [ ] Log muestra `[Idle]` ~20% de probabilidad entre grupos
- [ ] Clicks se ejecutan vía Emunium (con `move_to` + delay)
- [ ] Typing se ejecuta con delays entre caracteres (no instantáneo)
- [ ] Text variation con zero-width chars se aplica

## Detection & Evasion

- [ ] **CAPTCHA**: aparece → `_wait_for_manual_resolution` se activa
- [ ] **Soft-ban**: "temporarily blocked" → `_detect_challenge()` retorna `"banned"`, `_banned=True`
- [ ] **Checkpoint**: detectado como checkpoint → retries abortan
- [ ] Screenshots guardados en `screenshots/{account}/` en errores

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

- [ ] `job_store.save/load_cookies` funciona con formato Playwright
- [ ] `record_login` registra suceso en BD
- [ ] API `/post` y `/schedule` funcionan end-to-end
- [ ] Logs contienen prefijos correctos: `[Login]`, `[Publish]`, `[BANNED]`, `[Refresh]`, `[Idle]`, `[CAPTCHA]`
- [ ] `page.screenshot()` captura errores correctamente

## Configuration

- [ ] `browser_window_position = (0, 0)` sincroniza Emunium
- [ ] `emunium_enabled = True/False` toggle funciona
- [ ] `post_hours_allowed = range(0, 24)` para testing (REVERTIR antes de prod)
- [ ] Fallback a Patchright puro cuando `emunium_enabled = False`

## Edge Cases

- [ ] Grupo inválido → error graceful, continúa con siguiente
- [ ] Imagen corrupta → error capturado, continúa
- [ ] Red lenta → retries funcionan sin crash
- [ ] URL en texto → `_dismiss_link_preview` elimina card antes de publicar

---

## Notes

- **Critical:** Revert `post_hours_allowed` to `range(6, 23)` before production
- **Headless mode:** Set `browser_headless = True` + `emunium_enabled = False` for CI/Docker
- **Multiprocess:** Each process spawns own Patchright — expect slower startup than Selenium
- **Windows:** If Emunium fails (accessibility perms), fallback to `emunium_enabled = False` is automatic
