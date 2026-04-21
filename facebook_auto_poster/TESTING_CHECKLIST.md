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
- [x] Publicar texto + imagen en 1 grupo (zarai, imagen confirmada con thumbnail blob:)
- [x] Thumbnail confirmado: `//div[@role='dialog']//img[contains(@src,'blob:')]`
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

## Human Browsing & Gemini

### Warmup (human_browsing.py)
- [ ] `[Warmup] Iniciando warmup` aparece en log ~60% de los grupos
- [ ] Scroll en feed se ejecuta antes del compositor (ver `[Warmup] Scroll feed × N`)
- [ ] Hover sobre publicación dispara animación (verificar manualmente)
- [ ] Abrir hilo de comentarios y cerrar con Esc funciona sin escribir
- [ ] Excepción dentro del warmup NO interrumpe la publicación principal
- [ ] Tope `gemini_comment_max_per_session=2` se respeta
- [ ] Warmup solo corre en `attempt=1` (no se repite en reintentos)

### Gemini Commenter (gemini_commenter.py)
- [ ] Sin claves configuradas → log `[Gemini] Sin claves configuradas — desactivado`, no rompe init
- [ ] SDK `google-genai` no instalado → log `[Gemini] SDK google-genai no instalado`
- [ ] 1 clave válida → inicializa con `[Gemini] 1 clave(s) activa(s)`
- [ ] 2+ claves válidas → inicializa con `[Gemini] N clave(s) activa(s)`
- [ ] Clave con quota error (429) → marca con cooldown 300s, rota a siguiente
- [ ] Timeout duro de 60s: si API responde después, log `[Gemini] Timeout duro alcanzado`
- [ ] Comentario generado tiene < 200 chars y pasa sanitización
- [ ] Sanitización descarta respuestas con URL/email/hashtags/menciones a IA
- [ ] Comentario se postea en publicación ajena (no en propia)
- [ ] Imagen del post se descarga (max 4 MB) y se envía a Gemini multimodal
- [ ] Error no-quota devuelve None sin reintento (timeout duro = nunca bloquea)
- [ ] `gemini_comment_enabled=False` → módulo no se instancia
- [ ] Rotación automática entre múltiples claves funciona sin interrupción

### Smoke Test Aislado
```bash
python -c "from gemini_commenter import GeminiCommenter; \
  import logging, os; logging.basicConfig(level=logging.INFO); \
  g = GeminiCommenter(os.environ.get('GEMINI_API_KEY',''), 'gemini-2.5-flash', 15, 'es-MX', logging.getLogger('test')); \
  print(g.generate_comment('Vendo bici roja como nueva, $1500 negociable', None))"
```
Esperado: 1-2 frases coloquiales en español (ej. "Sigue disponible? me interesa").

---

## Bugs encontrados y corregidos

| # | Descripción | Estado |
|---|---|---|
| 1 | Publicación se hacía en campo de comentarios (selector muy genérico) | Corregido `0e98b89` |
| 2 | Loop infinito post-publicación (esperaba dialog genérico) | Corregido `0e98b89` |
| 3 | Event loop policy innecesaria en Python 3.13 Windows | Corregido `82162c0` |
| 4 | Imagen no se adjuntaba: faltaba click en botón Foto/Video antes de `set_input_files` | Corregido `7efbedb` |

## Notas

- **Cuenta carmen:** soft-ban activo (posts desaparecen al refrescar). Usar otra cuenta para continuar testing.
- **Critical:** Revert `post_hours_allowed` a `range(6, 23)` antes de producción.
- **Headless mode:** `browser_headless = True` + `emunium_enabled = False` para CI/Docker.
- **Multiprocess:** Cada proceso lanza su propio Patchright — startup más lento que Selenium.
