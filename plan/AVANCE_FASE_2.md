# Avance — Fase 2: Hardening

> Última actualización: 2026-04-23
> **Prerrequisito:** Fase 1 completada y con métricas en verde.

## Estado general

| # | Ítem | Estado | Completado |
|---|------|--------|-----------|
| 2.1 | `user_data_dir` persistente por cuenta | ⏳ Pendiente | — |
| 2.2 | Variación real de texto con Gemini (parafraseo) | ⏳ Pendiente | — |
| 2.3 | Pool de workers con límite de concurrencia | ⏳ Pendiente | — |
| 2.4 | Servidor de producción (waitress) | ⏳ Pendiente | — |
| 2.5 | Pin de dependencias + auditoría | ⏳ Pendiente | — |
| 2.6 | Rate limiter persistente (SQLite) | ⏳ Pendiente | — |
| 2.7 | Desactivación automática post-ban | ⏳ Pendiente | — |
| 2.8 | Healthcheck endpoint | ⏳ Pendiente | — |
| 2.9 | Descarga de imágenes no bloqueante | ⏳ Pendiente | — |

---

## Notas de dependencias

- **2.1** depende de **1.3** (fingerprint por cuenta para configurar el context correctamente)
- **2.2** requiere `gemini_commenter.py` exponga `generate_text()` (ya existe internamente)
- **2.3** depende de **2.4** (waitress primero para aislar problemas de threading)
- **2.7** depende de **2.1** (ban detection mejora con profile persistente)

---

## Métricas de validación de Fase 2 (al completar los 9 ítems)

| Métrica | Target | Estado |
|---------|--------|--------|
| Uptime del servidor | > 99% | Sin datos |
| Cuentas activas publicando | 100% de las configuradas | Sin datos |
| Diversidad textual (20 posts muestreados) | > 80% con variación real | Sin datos |
| Concurrencia API (10 req/s sostenido) | Sin degradación | Sin datos |
| Soft-bans auto-recuperados | < 1/cuenta/semana | Sin datos |
