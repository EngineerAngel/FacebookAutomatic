# Propuesta: Fase 2.10 — Auto-descubrimiento de grupos

> **Estado:** Propuesta  
> **Prioridad:** Media-Alta  
> **Tiempo estimado:** 2-3 días  
> **Dependencias:** Fases 1 + 2 completas  
> **Integración propuesta:** ANTES de Fase 3  

---

## Resumen ejecutivo

Sistema autónomo que **descubre grupos de Facebook nuevos** en horarios off-peak (12 PM - 8 AM) sin configuración manual. Los grupos descubiertos se almacenan en DB y están listos para publicaciones futuras.

**Resultado:** Expansión automática del alcance de cada cuenta. Hoy: grupos configurados manualmente. Mañana: descubrimiento continuo 24/7.

---

## Análisis: ¿Antes o después de Fase 3?

### Opción A — ANTES de Fase 3 (Recomendado) ✅

**Implementar como Fase 2.10 — pequeño ítem post-hardening**

**Ventajas:**
- ✅ Fases 1+2 ya tienen todo lo necesario (identidad por cuenta + estabilidad)
- ✅ No bloquea ni depende de Fase 3
- ✅ Complementa Fase 2 (hardening → expansión)
- ✅ Gana valor operacional **inmediatamente** (no esperar 3-4 semanas)
- ✅ Código sync actual funciona perfectamente (no necesita async aún)
- ✅ Se completa en 2-3 días, bajo riesgo
- ✅ Puede coexistir con Fase 3 sin interferencias

**Desventajas:**
- ⚠️ Sin tests formales (3.4 aún no hecho)
- ⚠️ Beneficiarse más de async post-Fase 3 (mejora de perf cuando migres a async)

---

### Opción B — DESPUÉS de Fase 3 (No recomendado) ❌

**Implementar en Fase 4**

**Ventajas:**
- ✅ Código async limpio (múltiples cuentas explorando en paralelo)
- ✅ Tests unitarios formales (3.4 hecho)
- ✅ Logging estructurado (3.3)

**Desventajas:**
- ❌ Retrasa valor operacional 1-2 meses más
- ❌ Fase 3 es larga (3-4 semanas) — esperar sería costoso
- ❌ No hay requisito técnico que lo bloquee — es decisión de timing

---

## **Recomendación final: FASE 2.10 (ANTES de Fase 3)**

**Razonamiento:**
1. Fases 1+2 lo soportan completamente
2. Bajo riesgo, bajo acoplamiento con Fase 3
3. Gana valor inmediato
4. Toma 2-3 días (no ralentiza a Fase 3)
5. Post-Fase 3, puedes optimizar a async si quieres (mejora de perf, no funcionalidad)

---

## Especificación técnica

### Flujo de descubrimiento

```
Scheduler de Fase 2.10
├─ Cada 12 horas (23:00-07:00 hora local de la cuenta)
├─ Para cada cuenta en is_active=1:
│  ├─ Instanciar FacebookPoster
│  ├─ Ir a https://www.facebook.com/groups/?category=joined
│  ├─ Ejecutar JS en consola (script de grupos.md)
│  ├─ Parsear resultado → [(nombre, id), ...]
│  ├─ INSERT INTO discovered_groups (account_name, group_id, group_name, discovered_at)
│  │  ON CONFLICT(account_name, group_id) DO UPDATE SET last_seen=NOW()
│  └─ Cerrar sesión
└─ Webhook callback: "X grupos nuevos descubiertos"
```

### Nueva tabla en DB

```sql
CREATE TABLE IF NOT EXISTS discovered_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL,
    group_id TEXT NOT NULL,
    group_name TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    added_to_posting BOOLEAN DEFAULT 0,  -- admin lo incluye manualmente
    last_seen TEXT NOT NULL,
    UNIQUE(account_name, group_id)
);

CREATE INDEX idx_discovered_account ON discovered_groups(account_name);
```

### Archivo: `group_discoverer.py`

```python
"""
group_discoverer.py — Descubrimiento autónomo de grupos de Facebook.

Corre en horarios off-peak (23:00-07:00 local).
Ejecuta el script de grupos.md via JavaScript en la consola del navegador.
Almacena resultados en `discovered_groups` tabla.

Referencia: plan/grupos.md (script DOM scraping seguro)
"""

import logging
from datetime import datetime
from config import load_accounts, is_account_hour_allowed
from facebook_poster import FacebookPoster
import job_store

logger = logging.getLogger("group_discoverer")

GROUPS_DISCOVERY_SCRIPT = """
// Script de plan/grupos.md — extraer grupos de cuenta actual
var links = document.querySelectorAll('a[href*="/groups/"]');
var results = [];
var seen = {};
var excluded = {'feed': 1, 'discover': 1, 'joins': 1, 'notifications': 1, 'pending': 1, 'joined': 1, 'category': 1, 'create': 1};
for (var i = 0; i < links.length; i++) {
  var href = links[i].href;
  var match = href.match(/groups\/([^/?#]+)/);
  if (!match) continue;
  var id = match[1];
  if (excluded[id] || seen[id]) continue;
  seen[id] = 1;
  var container = links[i].closest('div');
  var name = container ? container.innerText.split('\\n')[0].trim() : '(sin nombre)';
  results.push({nombre: name, id: id});
}
JSON.stringify(results);
"""

def discover_groups_for_account(account, config) -> list[dict]:
    """
    Descubre grupos de una cuenta ejecutando el script de grupos.md.
    Retorna: [{"id": "123456", "nombre": "Grupo X"}, ...]
    """
    poster = FacebookPoster(account, config)
    try:
        # Navegar a la página de grupos
        poster.page.goto("https://www.facebook.com/groups/?category=joined", timeout=30000)
        
        # Scroll para cargar todos los grupos (15 segundos)
        for _ in range(10):
            poster.page.evaluate("window.scrollBy(0, window.innerHeight)")
            poster.page.wait_for_timeout(1000)
        
        # Ejecutar script de extracción
        result_json = poster.page.evaluate(f"() => {GROUPS_DISCOVERY_SCRIPT}")
        
        logger.info(f"[{account.name}] Descubiertos {len(result_json)} grupos")
        return result_json
        
    except Exception as e:
        logger.error(f"[{account.name}] Error descubriendo grupos: {e}")
        return []
    finally:
        poster.close()

def sync_discovered_groups():
    """
    Sincroniza grupos descubiertos para todas las cuentas.
    Corre en horarios off-peak.
    """
    accounts = load_accounts()
    
    for account in accounts:
        # Solo durante horario permitido (off-peak para la cuenta)
        if is_account_hour_allowed(account):
            logger.info(f"[{account.name}] Fuera de horario de descubrimiento, skipped")
            continue
        
        groups = discover_groups_for_account(account, CONFIG)
        
        # Guardar en DB
        now = datetime.now().isoformat()
        for group in groups:
            job_store.upsert_discovered_group(
                account_name=account.name,
                group_id=group["id"],
                group_name=group["nombre"],
                discovered_at=now
            )
```

### Integración en scheduler

En `scheduler_runner.py`, añadir nueva tarea:

```python
from group_discoverer import sync_discovered_groups

def run():
    while True:
        # ... jobs pendientes de Fase 2 (como ahora)
        
        # NEW: cada 12 horas, descubrir grupos
        if should_run_discovery():
            sync_discovered_groups()
        
        time.sleep(30)
```

### Endpoint admin (optional, para triggear manual)

```python
@app.post("/admin/api/discover-groups/<account_name>")
@admin_required
def trigger_discovery(account_name):
    """Dispara descubrimiento manual para una cuenta."""
    job_store.create_job(
        text="",
        accounts=[account_name],
        job_type="discover_groups",
        ...
    )
    return jsonify({"status": "started"}), 202
```

---

## Validación y riesgos

### ¿Es seguro?

✅ **Sí. Razones:**
- Script solo lee DOM (sin API calls)
- No automatiza clics (no hay Selenium-like actions)
- No publica, no da like, no sigue a nadie
- Ejecuta en horarios off-peak (menor vigilancia)
- Es tu propia cuenta (no tercero)

**Referencia:** [plan/grupos.md — sección "¿Es seguro?"](./grupos.md#-es-seguro-te-pueden-banear)

### Riesgos técnicos

| Riesgo | Probabilidad | Mitigación |
|--------|-------------|-----------|
| Script rompe si FB cambia DOM | Media | Tests con snapshots DOM (Fase 3.4) |
| Descubre grupos privados bloqueados | Baja | DB marca `added_to_posting=0` hasta admin revise |
| Rate limiting del descubrimiento | Baja | Corre solo 1x cada 12h, 1 cuenta a la vez |
| Nombres grupo en caracteres no-ASCII | Muy baja | DB almacena UTF-8 nativo |

---

## Beneficios vs costo

| Aspecto | Valor |
|--------|-------|
| **Tiempo de implementación** | 2-3 días |
| **Complejidad técnica** | Baja (DOM scraping, no API) |
| **Riesgo de estabilidad** | Muy bajo |
| **Valor operacional** | Alto (expansión automática 24/7) |
| **Dependencias bloqueantes** | Ninguna |
| **Integración con Fase 3** | Nula (compatible sin cambios) |

---

## Plan de implementación

### Sprint 1 (1 día)
- [ ] Crear `group_discoverer.py` con core logic
- [ ] Agregar tabla `discovered_groups` a `job_store.init_db()`
- [ ] Tests: verificar script en un grupo real

### Sprint 2 (1 día)
- [ ] Integrar en `scheduler_runner.py`
- [ ] Endpoint `/admin/api/accounts/<name>/discovered-groups` (GET lista)
- [ ] Endpoint POST para mover grupo descubierto a lista de publicación

### Sprint 3 (1 día)
- [ ] Testing end-to-end con 2-3 cuentas
- [ ] Logs y observabilidad básica
- [ ] Documentación para admin

---

## Post-Fase 3 (mejora de perf, no bloqueante)

Una vez completes Fase 3 (async + observabilidad), puedes:
1. Reescribir `group_discoverer.py` a async → paralelizar descubrimiento entre cuentas
2. Agregar structured logging (Fase 3.3)
3. Snapshots DOM de "lista de grupos" para tests (Fase 3.4)

Esto es **mejora de performance**, no cambio funcional. La Fase 2.10 base funciona ahora.

---

## Referencias

- [plan/grupos.md](./grupos.md) — Script JavaScript de extracción segura
- [plan/AVANCE_FASE_1.md](./AVANCE_FASE_1.md) — Identidad por cuenta (prerequisito)
- [plan/AVANCE_FASE_2.md](./AVANCE_FASE_2.md) — Estabilidad (prerequisito)
- [facebook_auto_poster/scheduler_runner.py](../facebook_auto_poster/scheduler_runner.py) — Punto de integración
