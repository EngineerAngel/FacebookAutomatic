# Pendientes — tareas sin completar

> Items concretos que quedaron sin hacer al cerrar cada fase. No bloquean producción.

---

## ⬅️ PRÓXIMA TAREA — Config fixes en `config.py`

> **Prioridad:** Alta. Implementar en la sesión actual.

### P2.1 — Default timezone debería ser `"UTC"`
**Archivo:** [config.py:118](../facebook_auto_poster/config.py#L118)  
El default de `timezone` en `AccountConfig` es `"America/Mexico_City"`. Debería ser `"UTC"` para neutralidad. Solo afecta cuentas nuevas sin timezone explícito.  
**Esfuerzo:** 15 min

### P2.2 — `active_hours` no valida rangos inválidos
**Archivo:** [config.py:145](../facebook_auto_poster/config.py#L145)  
Valores como `[25, 30]` o `[23, 7]` no se detectan y causan que la cuenta nunca publique. Añadir validación + fallback a `(7, 23)` con WARNING.  
**Esfuerzo:** 20 min

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
