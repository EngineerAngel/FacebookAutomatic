# Pendientes — tareas sin completar

> Items concretos que quedaron sin hacer al cerrar cada fase. No bloquean producción.

---

## ✅ COMPLETADO — Config fixes en `config.py` (P2.1 + P2.2)

> **Prioridad:** Alta. **Status:** ✅ Implementado en commit [`2b6dc22`](https://github.com/EngineerAngel/FacebookAutomatic/commit/2b6dc22).

### P2.1 — Default timezone es ahora `"UTC"` ✅
**Archivo:** [config.py:118](../facebook_auto_poster/config.py#L118)  
Cambio completado. Default timezone en `AccountConfig` → `"UTC"` (en lugar de `"America/Mexico_City"`). Fallback en `load_accounts()` también actualizado.  
**Esfuerzo:** 15 min ✅

### P2.2 — `active_hours` ahora valida rangos ✅
**Archivo:** [config.py](../facebook_auto_poster/config.py)  
Implementado `_validate_active_hours()` helper. Detecta rangos inválidos (start > end, horas fuera [0-23]). Registra WARNING y fallback a `(7, 23)`. Se llama en `__post_init__` de todas las instancias. 5 tests nuevos, todos pasan.  
**Esfuerzo:** 20 min ✅

---

## Fingerprints — verificación manual pendiente (Fase 1.3)

La implementación está hecha pero **nunca se verificó con cuentas reales**:

- [ ] Verificar en https://bot.sannysoft.com/ con una cuenta real
- [ ] Verificar en https://amiunique.org/ que cada cuenta da hash único

---

## Group Discovery — testing E2E pendiente (Fase 2.10)

La feature está implementada pero **nunca fue probada en producción**:

- [ ] Crear 2-3 cuentas test en Facebook
- [ ] Verificar que descubrimiento encuentra grupos reales
- [ ] Verificar que botón ⏳ funciona y desaparece al terminar
- [ ] Verificar que grupos aparecen en tabla de descubiertos
- [ ] Verificar que "+ Añadir" añade grupo a `accounts.groups`
- [ ] Verificar que grupo aparece en tab "Configuración de cuentas"
- [ ] Verificar que se puede publicar en grupo descubierto añadido
- [ ] Verificar toast notifications (éxito y error)
- [ ] Verificar manejo de errores (FB rate limit, timeout, etc.)
- [ ] Verificar DB: tablas creadas, datos persistidos correctamente

**Próximos pasos post-testing:**
1. Métricas de grupos/min y tasa de error (observabilidad)
2. Scheduling en `scheduler_runner.py` para ejecución automática off-peak
3. Async rewrite para paralelizar descubrimiento entre cuentas
