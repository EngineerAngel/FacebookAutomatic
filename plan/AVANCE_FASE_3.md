# Avance — Fase 3: Refactor arquitectónico

> Última actualización: 2026-04-23
> **Prerrequisito:** Fases 1 y 2 completadas y estables por 2+ semanas.

## Estado general

| # | Ítem | Estado | Completado |
|---|------|--------|-----------|
| 3.1 | Migración a Playwright async + asyncio | ⏳ Pendiente | — |
| 3.2 | Flask → FastAPI con Pydantic | ⏳ Pendiente | — |
| 3.3 | Observabilidad (structlog + Prometheus) | ⏳ Pendiente | — |
| 3.4 | Tests unitarios + snapshots de DOM | ⏳ Pendiente | — |
| 3.5 | Eliminar lock global de SQLite | ⏳ Pendiente | — |
| 3.6 | Evaluar migración Emunium → humancursor/Camoufox | ⏳ Pendiente | — |
| 3.7 | Separar API de workers (procesos distintos) | ⏳ Pendiente | — |

---

## Notas

- **3.1** es prerequisito para **3.2** y **3.7**
- **3.5** es trivial y puede hacerse en cualquier momento (sin dependencias)
- **3.6** es un spike de investigación, no implementación directa
- Cada ítem de esta fase merece un branch separado y revisión cuidadosa

---

## Métricas de validación de Fase 3 (al completar)

| Métrica | Target | Estado |
|---------|--------|--------|
| Throughput | 5x más cuentas en mismo hardware | Sin datos |
| Latencia API p95 | < 100ms | Sin datos |
| Coverage de tests | > 60% en módulos puros | Sin datos |
| MTTR tras crash | < 1 minuto | Sin datos |
| Adaptación a cambio DOM de FB | < 1 hora | Sin datos |
