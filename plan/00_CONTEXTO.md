# 00 — Contexto del plan de mejora

## Objetivo global
Elevar el proyecto **Facebook Auto-Poster** a un estado donde:
1. **Riesgo de detección tienda a 0** (Facebook no pueda distinguir las cuentas de usuarios humanos reales ni correlacionarlas entre sí como un cluster).
2. **Rendimiento y estabilidad** soporten operación 24/7 con múltiples cuentas sin intervención manual.
3. **Mantenibilidad** permita evolucionar rápido cuando Facebook cambie su DOM o sus heurísticas de detección.

## Estado actual (auditoría al 2026-04-23)

### Stack existente
| Capa | Tecnología | Estado |
|------|-----------|--------|
| Browser automation | Patchright 1.58+ | ✓ Buena base anti-detección |
| Mouse/teclado OS | Emunium 3.0+ | ⚠ Mantenimiento limitado |
| Comentarios humanos | Google Gemini `gemini-2.5-flash` | ✓ Funcional, falta usarlo más |
| API | Flask 3+ con `app.run()` | ✗ No apto para producción |
| DB | SQLite WAL + lock global | ⚠ Lock redundante |
| Concurrencia | `threading.Thread` sin pool | ✗ Sin límites |
| Túnel | Cloudflared | ✓ OK |
| Python | 3.12 | ✓ OK |

### Riesgos de detección identificados
- **Fingerprint de hardware idéntico** entre cuentas (mismo Chromium, mismo viewport, misma locale, mismo UA hardcoded).
- **Sin proxy por cuenta**: todas las sesiones salen de la misma IP → cluster ban.
- **Password compartida** entre cuentas (`FB_PASSWORD` único).
- **User-Agent obsoleto** (Chrome 124 en época de Chrome 132+).
- **Variación de texto ineficaz** (solo zero-width chars, hash de texto coincide).
- **Ventana horaria abierta 24h** (`post_hours_allowed=range(0,24)` — comentario TODO dice revertir).
- **Typo rate 5%** (humanos: 1-2%).

### Riesgos arquitectónicos
- `sync_playwright` + threading no es thread-safe oficialmente.
- No hay límite de concurrencia global → riesgo OOM y sesiones paralelas idénticas visibles a FB.
- Rate limiter en memoria pierde estado al reiniciar.
- Cookies en texto plano en SQLite.
- Dependencias con `>=` (sin lock file).
- `app.run()` de Flask en producción.
- Recuperación post-ban incompleta (loguea pero no desactiva cuenta).

## Criterios de éxito por fase

### Fase 1 — Stop-the-bleeding (semana 1)
**Meta:** Cada cuenta parece un usuario único e independiente a nivel de red y de dispositivo básico.
- Proxy residencial sticky por cuenta.
- Password individual cifrada.
- UA + viewport + locale + timezone únicos por cuenta.
- Ventana horaria realista (6-22h en timezone local de la cuenta).
- Typo rate bajado a 1-2%.

### Fase 2 — Hardening (semanas 2-3)
**Meta:** Persistencia de identidad completa + robustez operacional.
- `user_data_dir` persistente por cuenta.
- Variación de texto real vía Gemini (parafraseo).
- Concurrencia controlada con pool.
- Servidor de producción (`waitress`).
- Dependencias fijadas y auditadas.
- Cookies cifradas con Fernet.

### Fase 3 — Refactor arquitectónico (mes 2+)
**Meta:** Base técnica moderna para evolución rápida.
- Migración a Playwright async + asyncio.
- Flask → FastAPI con validación Pydantic.
- Observabilidad (logs estructurados + métricas Prometheus).
- Tests unitarios + snapshots de DOM.

## Principios de trabajo

1. **Cada cambio se valida en staging** antes de producción. Una detección acelera el ban de cuentas reales.
2. **Un cambio a la vez.** No mezclar fixes anti-detección con refactors.
3. **Medible:** cada fase debe tener métricas (tasa de login exitoso, tasa de publicación, tasa de soft-bans detectados).
4. **Reversible:** cambios con feature flags en `CONFIG` para rollback rápido.
5. **Mantener compatibilidad con OpenClaw** — los endpoints públicos no cambian de contrato sin coordinación.

## Documentos del plan

- [01_FASE_1.md](01_FASE_1.md) — Crítica (stop-the-bleeding)
- [02_FASE_2.md](02_FASE_2.md) — Hardening
- [03_FASE_3.md](03_FASE_3.md) — Refactor arquitectónico

## Estado de fases (2026-04-24)

| Fase | Estado | Notas |
|------|--------|-------|
| Fase 1 | ✅ 5/6 completos | 1.1 (proxies) pendiente — necesita hardware |
| Fase 2 | ✅ 9/9 completos | Integrada en master. Pendiente pruebas formales. |
| Fase 3 | ⏳ No iniciada | Prerrequisito: Fases 1+2 estables 2+ semanas |
| Auditoría | ✅ Cerrada | 8/9 resueltos + 1 ya no aplica (función eliminada) |

## Fuentes de verdad
- Código: `c:\Users\ag464\Desktop\PublicWeb\facebook_auto_poster\`
- DB: `facebook_auto_poster\jobs.db` (SQLite)
- Config: `config.py` + `.env`
- Logs: `facebook_auto_poster\logs\main.log` + `logs\{account}.log`
