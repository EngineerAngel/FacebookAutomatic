# Plan de mejora — Índice

> Última actualización: 2026-05-03

## Contexto y estado

| Documento | Contenido |
|-----------|-----------|
| [00_CONTEXTO.md](00_CONTEXTO.md) | Objetivo global, stack actual, riesgos vigentes, criterios de éxito por fase |

## Estado de fases

Todas las fases completadas. Ver [00_CONTEXTO.md](00_CONTEXTO.md) para riesgos vigentes.

| Fase | Estado | Resumen |
|------|--------|---------|
| Fase 1 | ✅ Completa | Fingerprints, passwords cifradas, timezone por cuenta, typo rate, cross-platform |
| Fase 2 | ✅ Completa | Waitress, Gemini, rate limiter SQLite, ban cooldown, healthcheck |
| Fase 2.10 | ✅ Completa | Auto-descubrimiento de grupos (pendiente testing E2E — ver PENDIENTES.md) |
| Fase 3 | ✅ Completa | Async-only, FastAPI /v2, structlog, Prometheus, DOM repair con Scrapling |

## Referencia técnica

| Documento | Contenido |
|-----------|-----------|
| [ORCHESTRATOR_DESIGN.md](ORCHESTRATOR_DESIGN.md) | **Nuevo.** Diseño técnico del orquestador de sesiones — async-first, 6 fases independientes (A→F), integración con worker actual |
| [ORCHESTRATOR_REVIEW.md](ORCHESTRATOR_REVIEW.md) | **Nuevo.** Revisión de fallos y rediseños — 11 fallos mapeados, estado actual, reglas estrictas |
| [SCRAPLING_REFERENCE.md](SCRAPLING_REFERENCE.md) | Referencia técnica de Scrapling para DOM repair (3.4) |
| [ANTIDETECCION_COMPORTAMIENTO_HUMANO.md](ANTIDETECCION_COMPORTAMIENTO_HUMANO.md) | Análisis de capas de detección de Facebook y gaps |

## Tareas pendientes

| Documento | Contenido |
|-----------|-----------|
| [PENDIENTES.md](PENDIENTES.md) | Items concretos sin completar: config fixes, fingerprint verification, group discovery E2E |

## Guías operacionales

| Documento | Contenido |
|-----------|-----------|
| [grupos.md](grupos.md) | Script JS para extracción segura de IDs de grupos desde el browser |

## Contexto para sesiones activas

| Documento | Propósito |
|-----------|-----------|
| [CONTEXTO_PROXIES_SIGUIENTE_CHAT.md](CONTEXTO_PROXIES_SIGUIENTE_CHAT.md) | Histórico: migración proxy `produccion_temp → fase-3` (✅ completada) |
