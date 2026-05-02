# 00 — Contexto del plan de mejora

> **Última actualización:** 2026-05-01

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
| API | Flask 3+ + Waitress 3.0 | ✅ Producción — FastAPI en `/v2` pendiente (3.2) |
| DB | SQLite WAL, `busy_timeout=5s`, sin lock Python | ✅ Limpio (Fase 3.5) |
| Concurrencia | `asyncio` + `asyncio.Semaphore` | ✅ Async-only (Fase 3.1) |
| Logs | texto (default) o structlog JSON (`STRUCTURED_LOGGING=1`) | ✅ Dual-mode (Fase 3.3a) |
| Túnel | Cloudflared | ✅ OK |
| Python | 3.12 | ✅ OK |

---

## Riesgos vigentes

| Severidad | Riesgo | Estado |
|-----------|--------|--------|
| 🔴 Crítico | Sin proxy por cuenta — cluster-ban risk | 🔄 En progreso (otra rama) |
| 🟡 Medio | Cookies de sesión en texto plano en SQLite | ⏳ Planificado post-Fase 3 |

Los demás riesgos del diagnóstico inicial están resueltos: fingerprints únicos (1.3), passwords cifradas (1.2), ventana horaria por timezone (1.4), typo rate realista (1.5), Waitress en producción (Fase 2), rate limiter SQLite-backed (Fase 2), migración async (3.1).

---

## Criterios de éxito por fase

### Fase 1 — Stop-the-bleeding
✅ **Completada.** Cada cuenta tiene identidad única: proxy (en progreso), password cifrada, fingerprint UA+viewport+locale, ventana horaria, typo rate realista.

### Fase 2 — Hardening
✅ **Completada.** Persistencia completa de identidad, variación de texto real vía Gemini, concurrencia controlada, Waitress, dependencias pinadas, rate limiter SQLite. Incluye Fase 2.10 (auto-descubrimiento de grupos).

### Fase 3 — Refactor arquitectónico
🔄 **En progreso.** Async migration ✅, structlog ✅, SQLite WAL ✅. Pendiente: FastAPI (3.2), Prometheus (3.3b), DOM repair con Scrapling+Gemini (3.4).

---

## Principios de trabajo

1. **Cada cambio se valida en staging** antes de producción. Una detección acelera el ban de cuentas reales.
2. **Un cambio a la vez.** No mezclar fixes anti-detección con refactors.
3. **Medible:** cada fase tiene métricas (tasa de login, publicación, soft-bans).
4. **Reversible:** cambios con feature flags en `CONFIG` — rollback sin `git revert`.
5. **Compatibilidad con OpenClaw** — endpoints públicos no cambian de contrato sin coordinación.

---

## Documentos activos del plan

| Documento | Contenido |
|-----------|-----------|
| [AVANCE_FASE_1.md](AVANCE_FASE_1.md) | Estado Fase 1 — 5/6 completos, 1.1 en otra rama |
| [AVANCE_FASE_2.md](AVANCE_FASE_2.md) | Estado Fase 2 — 9/9 completos |
| [AVANCE_FASE_2_10.md](AVANCE_FASE_2_10.md) | Auto-descubrimiento de grupos — completo |
| [AVANCE_FASE_3.md](AVANCE_FASE_3.md) | Estado Fase 3 — tracking activo |
| [03_FASE_3.md](03_FASE_3.md) | Especificación técnica de Fase 3 |
| [DECISION_3.2_FASTAPI.md](DECISION_3.2_FASTAPI.md) | Decisión de orden de implementación: 3.2 → 3.3b → 3.4 |
| [SCRAPLING_REFERENCE.md](SCRAPLING_REFERENCE.md) | Referencia técnica de Scrapling para implementar en 3.4 |
| [ANTIDETECCION_COMPORTAMIENTO_HUMANO.md](ANTIDETECCION_COMPORTAMIENTO_HUMANO.md) | Análisis de capas de detección de Facebook |
| [grupos.md](grupos.md) | Script JS para extracción segura de grupos |

---

## Fuentes de verdad

- Código: `facebook_auto_poster/`
- DB: `facebook_auto_poster/jobs.db` (SQLite)
- Config: `config.py` + `.env`
- Logs: `logs/main.log` + `logs/{account}.log`
