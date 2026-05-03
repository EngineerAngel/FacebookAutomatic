# 00 — Contexto del plan de mejora

> **Última actualización:** 2026-05-03 (revisión documentación — proxies marcados completos)

## Objetivo global

Elevar el proyecto **Facebook Auto-Poster** a un estado donde:
1. **Riesgo de detección tienda a 0** — Facebook no pueda distinguir las cuentas de usuarios humanos reales ni correlacionarlas entre sí como un cluster.
2. **Rendimiento y estabilidad** soporten operación 24/7 con múltiples cuentas sin intervención manual.
3. **Mantenibilidad** permita evolucionar rápido cuando Facebook cambie su DOM o sus heurísticas de detección.

---

## Stack actual (2026-05-01)

| Capa | Tecnología | Estado |
|------|-----------|--------|
| Browser automation | Patchright 1.58+ (async) | ✅ Async migrado (Fase 3.1) |
| Mouse/teclado OS | Emunium 3.0+ via `asyncio.to_thread` | ✅ Funcional |
| Comentarios humanos | Google Gemini `gemini-2.5-flash` | ✅ Funcional |
| API principal | Flask 3+ (puerto `/`) + FastAPI en `/v2` | ✅ Ambos activos — FastAPI con `USE_FASTAPI=1` (Fase 3.2) |
| Validación de entrada | Pydantic v2 en `/v2/*` endpoints | ✅ Activo (Fase 3.2) |
| DB | SQLite WAL, `busy_timeout=5s`, sin lock Python | ✅ Limpio (Fase 3.5) |
| Concurrencia | `asyncio` + `asyncio.Semaphore` | ✅ Async-only (Fase 3.1) |
| Logs | texto (default) o structlog JSON (`STRUCTURED_LOGGING=1`) | ✅ Dual-mode (Fase 3.3a) |
| Métricas | Prometheus `/metrics` + Grafana (Docker opcional) | ✅ Activo con `METRICS_ENABLED=1` (Fase 3.3b) |
| DOM repair | Scrapling (adaptativo) + Gemini (fallback) + aprobación admin | ✅ Activo con `ADAPTIVE_SELECTORS=1` (Fase 3.4) |
| Procesos | API (`api_main.py`) y Worker (`worker_main.py`) separables | ✅ Separados (Fase 3.7) |
| Túnel | Cloudflared | ✅ OK |
| Python | 3.12 | ✅ OK |

---

## Riesgos vigentes

| Severidad | Riesgo | Estado |
|-----------|--------|--------|
| ✅ Resuelto | Sin proxy por cuenta — cluster-ban risk | ✅ Completo (commits `5938d43`, `980b2c5`, `9f2aed1`). `resolve_proxy()` con asignación dinámica LRU, `MAX_ACCOUNTS_PER_NODE=10`, cooldown entre rotaciones. |
| 🟡 Medio | Cookies de sesión en texto plano en SQLite | ⏳ Planificado post-Fase 3 |

Los demás riesgos del diagnóstico inicial están resueltos: fingerprints únicos (1.3), passwords cifradas (1.2), ventana horaria por timezone (1.4), typo rate realista (1.5), Waitress en producción (Fase 2), rate limiter SQLite-backed (Fase 2), migración async (3.1).

---

## Criterios de éxito por fase

### Fase 1 — Stop-the-bleeding
✅ **Completada.** Cada cuenta tiene identidad única: proxy (en progreso), password cifrada, fingerprint UA+viewport+locale, ventana horaria, typo rate realista.

### Fase 2 — Hardening
✅ **Completada.** Persistencia completa de identidad, variación de texto real vía Gemini, concurrencia controlada, Waitress, dependencias pinadas, rate limiter SQLite. Incluye Fase 2.10 (auto-descubrimiento de grupos).

### Fase 3 — Refactor arquitectónico
✅ **Completada (2026-05-01).** Async-only (3.1), FastAPI `/v2` (3.2), structlog (3.3a), Prometheus+Grafana (3.3b), DOM repair Scrapling+Gemini (3.4), SQLite WAL (3.5), API/Worker separados (3.7). Ítem 3.6 (spike mouse library) descartado — Emunium funciona en producción.

---

## Principios de trabajo

1. **Cada cambio se valida en staging** antes de producción. Una detección acelera el ban de cuentas reales.
2. **Un cambio a la vez.** No mezclar fixes anti-detección con refactors.
3. **Medible:** cada fase tiene métricas (tasa de login, publicación, soft-bans).
4. **Reversible:** cambios con feature flags en `CONFIG` — rollback sin `git revert`.
5. **Compatibilidad con OpenClaw** — endpoints públicos no cambian de contrato sin coordinación.

---

## Documentos del plan

| Documento | Contenido |
|-----------|-----------|
| [PENDIENTES.md](PENDIENTES.md) | Tareas concretas sin completar (config fixes, fingerprint verification, group discovery E2E) |
| [CONTEXTO_PROXIES_SIGUIENTE_CHAT.md](CONTEXTO_PROXIES_SIGUIENTE_CHAT.md) | ~~Migración pendiente~~ Migración completada (Bloques A-D). Documento histórico. |
| [SCRAPLING_REFERENCE.md](SCRAPLING_REFERENCE.md) | Referencia técnica del sistema de DOM repair (Scrapling + Gemini) — ya implementado |
| [ANTIDETECCION_COMPORTAMIENTO_HUMANO.md](ANTIDETECCION_COMPORTAMIENTO_HUMANO.md) | Análisis de capas de detección de Facebook y gaps pendientes |
| [grupos.md](grupos.md) | Script JS para extracción segura de IDs de grupos desde el browser |

---

## Fuentes de verdad

- Código: `facebook_auto_poster/`
- DB: `facebook_auto_poster/jobs.db` (SQLite)
- Config: `config.py` + `.env`
- Logs: `logs/main.log` + `logs/{account}.log`
