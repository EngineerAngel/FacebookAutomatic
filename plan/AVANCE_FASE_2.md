# Avance — Fase 2: Hardening

> Última actualización: 2026-04-24
> **Estado:** ✅ Completada — integrada en `master` el 2026-04-24 (rama `lap2`)
> **Pendiente:** pruebas formales + revisión de calidad post-merge

## Estado general

| # | Ítem | Estado | Completado |
|---|------|--------|-----------|
| 2.1 | `user_data_dir` persistente por cuenta | ✅ Completado | 2026-04-24 |
| 2.2 | Variación real de texto con Gemini (parafraseo) | ✅ Completado | 2026-04-24 |
| 2.3 | Pool de workers con límite de concurrencia | ✅ Completado | 2026-04-24 |
| 2.4 | Servidor de producción (waitress) | ✅ Completado | 2026-04-24 |
| 2.5 | Pin de dependencias + auditoría | ✅ Completado | 2026-04-24 |
| 2.6 | Rate limiter persistente (SQLite) | ✅ Completado | 2026-04-24 |
| 2.7 | Desactivación automática post-ban | ✅ Completado | 2026-04-24 |
| 2.8 | Healthcheck endpoint | ✅ Completado | 2026-04-24 |
| 2.9 | Descarga de imágenes no bloqueante | ✅ Completado | 2026-04-24 |

---

## Notas de implementación

- **2.1** `user_data_dir` persistente por cuenta con fingerprint genérico como fallback si no hay fingerprint asignado.
- **2.2** Parafraseo real con Gemini `gemini-2.5-flash` segmentado por `(cuenta, grupo)` para máxima variación textual.
- **2.3** `ThreadPoolExecutor` con `MAX_CONCURRENT_WORKERS` (default 2) + locks por cuenta para evitar sesiones paralelas idénticas.
- **2.4** Waitress reemplaza `app.run()` + graceful shutdown + recuperación de jobs huérfanos al arrancar.
- **2.5** Dependencias pinadas con `~=` en `requirements.txt` + `waitress` añadido.
- **2.6** Rate limiter migrado de `defaultdict` en memoria → tabla SQLite `rate_events`. Sobrevive reinicios. Resuelve también el memory leak de P2-1 de la auditoría.
- **2.7** Cooldown de 48h post-ban con desactivación automática de la cuenta. Requiere 2.1 para detección más precisa de perfiles.
- **2.8** Endpoints `/health` (público) y `/health/detailed` (requiere API key) con estado de workers, DB y scheduler.
- **2.9** Descarga de imagen URL con `ThreadPoolExecutor` — no bloquea el thread principal del job.

---

## Métricas de validación de Fase 2

| Métrica | Target | Estado |
|---------|--------|--------|
| Uptime del servidor | > 99% | Pendiente datos de producción |
| Cuentas activas publicando | 100% de las configuradas | Pendiente datos de producción |
| Diversidad textual (20 posts muestreados) | > 80% con variación real | Pendiente datos de producción |
| Concurrencia API (10 req/s sostenido) | Sin degradación | Pendiente datos de producción |
| Soft-bans auto-recuperados | < 1/cuenta/semana | Pendiente datos de producción |
