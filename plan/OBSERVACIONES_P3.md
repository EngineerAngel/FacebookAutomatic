# Observaciones P3 — Revisión de código 1.4 y 1.5

> **Prioridad**: Baja | **Costo**: Bajo | **Impacto**: Bajo

---

## P3.1 — Funciones puras sin cobertura de tests

### Observación
Las funciones `is_account_hour_allowed()` y `_human_type()` son **puras** (deterministas, sin side effects) pero carecen de test cases:

**[config.py:113-117](../facebook_auto_poster/config.py#L113-L117)** — `is_account_hour_allowed()`
```python
def is_account_hour_allowed(account: AccountConfig) -> bool:
    """Verifica si la hora local de la cuenta está dentro de su ventana de publicación."""
    local_hour = datetime.now(ZoneInfo(account.timezone)).hour
    start, end = account.active_hours
    return start <= local_hour < end
```

**[facebook_poster.py:207-254](../facebook_auto_poster/facebook_poster.py#L207-L254)** — `_human_type()`
```python
def _human_type(self, text: str) -> None:
    """Escribe texto de manera humana: typos, correcciones, delays variables."""
    # ...
```

### Riesgo — **Bajo en producción, Medio en evolución**
- **DST transitions**: `ZoneInfo` maneja DST correctamente, pero edge cases (2am en primavera) no están probados
- **Boundary testing**: ¿Qué pasa a las 23:00 cuando `active_hours=(7, 23)`? Debería retornar False. No verificado.
- **Typing patterns**: `_human_type()` es estocástica. Los parámetros `lognormvariate` no están validados contra datos reales de typing humano
- **Regresión futura**: Si alguien optimiza estas funciones, sin tests pueden romperse silenciosamente

### Propuesta

**Mínimo (recomendado para ahora):**
```python
# tests/test_config.py
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from config import is_account_hour_allowed, AccountConfig

def test_is_account_hour_allowed_within_window():
    """Hora dentro de ventana."""
    account = AccountConfig(
        name="test", email="test@test.com", password="",
        timezone="UTC", active_hours=(7, 23)
    )
    # Mock datetime para control determinístico
    with patch("config.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 4, 23, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert is_account_hour_allowed(account) is True

def test_is_account_hour_allowed_outside_window():
    """Hora fuera de ventana."""
    account = AccountConfig(
        name="test", email="test@test.com", password="",
        timezone="UTC", active_hours=(7, 23)
    )
    with patch("config.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 4, 23, 3, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert is_account_hour_allowed(account) is False

def test_is_account_hour_allowed_boundary_start():
    """Exactamente a la hora de inicio."""
    account = AccountConfig(
        name="test", email="test@test.com", password="",
        timezone="UTC", active_hours=(7, 23)
    )
    with patch("config.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 4, 23, 7, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert is_account_hour_allowed(account) is True

def test_is_account_hour_allowed_boundary_end():
    """Exactamente a la hora de fin (debería ser False)."""
    account = AccountConfig(
        name="test", email="test@test.com", password="",
        timezone="UTC", active_hours=(7, 23)
    )
    with patch("config.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 4, 23, 23, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert is_account_hour_allowed(account) is False

def test_timezone_conversion():
    """Timezone distinto del servidor."""
    # Account en México, servidor en UTC
    account = AccountConfig(
        name="test", email="test@test.com", password="",
        timezone="America/Mexico_City", active_hours=(7, 23)
    )
    # Si es las 12:00 UTC = 6:00 AM (CDT: UTC-5) → fuera de ventana
    with patch("config.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 4, 23, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert is_account_hour_allowed(account) is False
```

**Óptimo (más adelante):**
- Test estocástico para `_human_type()`: capturar delays reales, verificar que `lognormvariate` produce distribución correcta
- Test para edge cases de DST (cambio de hora en primavera/otoño)

### Viabilidad
- **Esfuerzo**: 45 minutos (crear `tests/` + 5 test cases + fixture setup)
- **Riesgo de regresión**: Ninguno (solo lectura de código existente)
- **ROI**: Bajo ahora (código es simple), medio en 6 meses (protección contra cambios)
- **Recomendación**: **Implementar cuando agregues CI/CD**, o posponer si no hay pipeline de tests. No es bloqueante para 1.1-1.3.

---

## P3.2 — Optimización menor de imports

### Observación
En [account_manager.py:1-14](../facebook_auto_poster/account_manager.py#L1-L14), hay un patrón de imports que puede limpiarse:

```python
import logging
import multiprocessing
import random
import time
from multiprocessing.managers import DictProxy

from config import AccountConfig, is_account_hour_allowed
from facebook_poster import FacebookPoster
```

**No hay desperdicio**, pero hay un import silenciosamente unused (potencial):
- Si en el futuro se elimina la clase `DictProxy`, el import muere
- `random` solo se usa en `run_sequential()` — podría documentarse

### Propuesta
Agregar comentario de claridad (opcional):
```python
import logging
import multiprocessing
import random  # Para delay entre cuentas
import time

from multiprocessing.managers import DictProxy
from config import AccountConfig, is_account_hour_allowed
from facebook_poster import FacebookPoster
```

Alternativa: Usar `__all__` para explicitar dependencias:
```python
__all__ = ["AccountManager"]
```

### Viabilidad
- **Esfuerzo**: 2 minutos (agregar 1 comentario)
- **Riesgo**: Ninguno
- **Impacto**: Cosmético
- **Recomendación**: **Skip para ahora**. No mejora legibilidad ni mantenibilidad. La estructura actual es clara. Hacer esto cuando refactorices broader.

---

## Resumen P3

| Ítem | Acción | Esfuerzo | Prioridad | ROI |
|------|--------|----------|-----------|-----|
| P3.1 | Agregar tests para funciones puras | 45 min | Baja | Bajo→Medio |
| P3.2 | Comentarios de claridad en imports | 2 min | Nula | Cosmético |

**Recomendación**: 
- **P3.1**: Implementar cuando tengas CI/CD, o si descubres bugs en `is_account_hour_allowed()` bajo DST
- **P3.2**: No necesario. Skip.

---

## Próximos pasos sugeridos
1. **Revisar P2** — ¿Implementar P2.1 + P2.2 antes de producción?
2. **Implementar 1.1** (proxy pool) — siguiente ítem crítico
3. **P3.1** — Agregar cuando haya proyecto de testing
