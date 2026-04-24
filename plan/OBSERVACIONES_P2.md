# Observaciones P2 — Revisión de código 1.4 y 1.5

> **Prioridad**: Media | **Costo**: Bajo | **Impacto**: Moderado

---

## P2.1 — Timezone DDL demasiado opinionado

### Observación
En [config.py:100](../facebook_auto_poster/config.py#L100), el default de timezone está hardcodeado:
```python
timezone: str = "America/Mexico_City"
```

Este valor aparece en tres lugares:
1. Default de `AccountConfig.timezone`
2. Fallback en `load_accounts()` línea 153
3. Asunción implícita en la documentación

### Riesgo
- **Portabilidad**: Si el proyecto se usa en otra región, requiere modificar código fuente
- **Confusión operacional**: Un admin podría asumir que omitir timezone usa el servidor local, pero obtiene México City
- **Migraciones**: En producción, cambiar esto es un breaking change

### Propuesta
Usar uno de estos enfoques:

**Opción A (Recomendado):** Default a UTC + requerir explícito por cuenta
```python
timezone: str = "UTC"  # Neutral, fácil de validar, documentado
```
Ventaja: Força explicititud en la configuración. Admin ve claramente qué timezone es cada cuenta.

**Opción B:** Leer del servidor (requiere `TZ` env var en deploy)
```python
timezone: str = os.getenv("DEFAULT_TIMEZONE", "UTC")
```
Ventaja: Flexible, respeta la config del servidor.

**Opción C:** Dejar requerido (sin default)
```python
timezone: str  # Sin default
```
Requeriría validación en `load_accounts()` para fallar explícitamente si falta.

### Viabilidad
- **Esfuerzo**: 15 minutos (cambiar 3 líneas + docs)
- **Riesgo de regresión**: Muy bajo (solo afecta fallback, DB ya tiene valores)
- **Recomendación**: **Implementar Opción A** antes del deploy a producción. Si ya hay datos en DB con timezone, esto no afecta. Solo afecta cuentas nuevas sin timezone configurado.

---

## P2.2 — Validación ausente en `active_hours` JSON

### Observación
En [config.py:145-146](../facebook_auto_poster/config.py#L145-L146), se parsea `active_hours` sin validar rango:
```python
active_hours_raw = r.get("active_hours") or "[7, 23]"
active_hours = tuple(json.loads(active_hours_raw))  # ← Sin validación
```

### Riesgo
Un admin podría insertar en DB (via API o SQL) valores inválidos:
- `"[25, 30]"` → horas > 23, nunca match en `is_account_hour_allowed()`
- `"[23, 7]"` → rango invertido, nunca match
- `"[7]"` → JSON malformado, `tuple()` falla silenciosamente
- `"invalid"` → JSON inválido, exception no capturada

**Impacto**: Si falla `load_accounts()`, toda la sesión se detiene.

### Propuesta
Agregar validación en `config.py`:

```python
def _validate_active_hours(hours: tuple[int, int]) -> None:
    """Valida que active_hours sea un rango válido [start, end) con 0 <= start < end <= 24."""
    if len(hours) != 2:
        raise ValueError(f"active_hours debe tener exactamente 2 elementos, got {len(hours)}")
    start, end = hours
    if not (0 <= start < end <= 24):
        raise ValueError(f"active_hours [{start}, {end}) fuera de rango válido [0-24)")

# En load_accounts(), línea 146:
try:
    active_hours = tuple(json.loads(active_hours_raw))
    _validate_active_hours(active_hours)
except (json.JSONDecodeError, ValueError) as e:
    logger.warning(
        "Invalid active_hours %r para cuenta %s, using default [7, 23]",
        active_hours_raw, r["name"]
    )
    active_hours = (7, 23)
```

### Viabilidad
- **Esfuerzo**: 20 minutos (función + try/except + testing)
- **Riesgo de regresión**: Muy bajo (solo "dureciza" lo que ya debería ser verdad)
- **Recomendación**: **Implementar esta validación**. Protege contra datos corruptos y proporciona feedback claro al admin. Costo negligible.

---

## Resumen P2

| Ítem | Acción | Esfuerzo | Prioridad | Riesgo |
|------|--------|----------|-----------|--------|
| P2.1 | Cambiar default timezone a UTC | 15 min | Media | Muy bajo |
| P2.2 | Validar rango de active_hours | 20 min | Media | Muy bajo |

**Recomendación**: Implementar ambas. Mejoran robustez sin costos significativos. Ideales para antes del deploy a producción.
