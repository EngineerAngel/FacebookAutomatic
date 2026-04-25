# AVANCE — Fase 2.10: Auto-descubrimiento de grupos

**Estado:** ✅ COMPLETADO (2026-04-25)  
**Duración:** 1 sesión  
**Commit:** `feat(2.10): Auto-descubrimiento de grupos con trigger manual en admin panel`

---

## Resumen ejecutivo

Implementada Fase 2.10 — Sistema de **descubrimiento autónomo de grupos de Facebook** con:
- ✅ Trigger manual desde panel admin (botón 🔍 por cuenta)
- ✅ Polling en tiempo real con notificaciones toast
- ✅ Gestión de grupos descubiertos (aprobación antes de añadir a publicación)
- ✅ Seguridad: DOM scraping puro, sin API calls, sin automatización de clics
- ⏳ Pendiente: Pruebas end-to-end con cuentas reales

---

## Qué se implementó

### 1. Módulo de descubrimiento (`group_discoverer.py`)

```python
discover_groups_for_account(account, config) → list[dict]
```

**Flujo:**
1. Abre sesión de FacebookPoster para la cuenta
2. Navega a `https://www.facebook.com/groups/?category=joined`
3. Hace scroll 10 veces (1s cada uno) para cargar grupos dinámicamente
4. Ejecuta JavaScript seguro de `plan/grupos.md`:
   - Busca todos los links con `/groups/` en href
   - Excluye slugs del sistema (feed, discover, joins, notifications, etc.)
   - Extrae ID de grupo y nombre del contenedor más cercano
   - Retorna `[{id: "123456", name: "Grupo X"}, ...]`
5. Cierra la sesión en el `finally`

**Seguridad:**
- ✓ Solo lectura del DOM (no publica, no da like, no automatiza clics)
- ✓ Sin API calls (no toca endpoints de Facebook)
- ✓ Se ejecuta en sesión autenticada de la cuenta (no es tercero)
- ✓ Puede ejecutarse en cualquier horario (aunque se recomienda off-peak)

### 2. Persistencia en DB (`job_store.py`)

**Nuevas tablas:**

```sql
discovery_runs {
  id: TEXT PK
  account_name: TEXT
  status: TEXT (running | done | failed)
  started_at: TEXT (ISO 8601)
  finished_at: TEXT (ISO 8601, NULL si running)
  groups_found: INTEGER
  error: TEXT (primeros 500 chars si failed)
}

discovered_groups {
  id: INTEGER PK AUTOINCREMENT
  account_name: TEXT
  group_id: TEXT
  group_name: TEXT
  discovered_at: TEXT (ISO 8601)
  added_to_posting: INTEGER (0 | 1, default 0)
  last_seen: TEXT (ISO 8601)
  UNIQUE(account_name, group_id)
}
```

**7 Nuevas funciones CRUD:**
- `create_discovery_run(run_id, account_name)` — inicia nuevo run
- `finish_discovery_run(run_id, groups_found)` — marca done + cuenta
- `fail_discovery_run(run_id, error)` — marca failed + error (truncado a 500 chars)
- `get_discovery_run(run_id)` → dict | None — polling
- `upsert_discovered_group(account_name, group_id, group_name, discovered_at)` — guarda o actualiza
- `list_discovered_groups(account_name)` → list[dict] — lista pendientes primero
- `mark_group_added_to_posting(account_name, group_id)` → bool — marca como aprobado

### 3. API REST (`api_server.py`)

**Helper function:**
```python
_run_discovery(run_id: str, account_name: str) → None
```
Ejecuta en thread daemon:
1. Busca cuenta en configuración
2. Llama `discover_groups_for_account()`
3. Inserta resultados en `discovered_groups` con timestamp actual
4. Actualiza `discovery_runs` a `done` con count
5. Maneja errores → `fail_discovery_run(run_id, error_msg)`
6. Logs a INFO/ERROR

**5 Nuevos endpoints:**

| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/admin/api/accounts/<name>/discover-groups` | Inicia descubrimiento. Retorna `{run_id, status: "running"}` (202) |
| GET | `/admin/api/discovery/<run_id>` | Polling del estado. Retorna objeto `discovery_runs` |
| GET | `/admin/api/accounts/<name>/discovered-groups` | Lista grupos descubiertos + estadísticas (pending count) |
| POST | `/admin/api/accounts/<name>/discovered-groups/<group_id>/add` | Añade grupo a `accounts.groups` + marca `added_to_posting=1` |

Todos requieren autenticación admin (@admin_required).

### 4. UI Admin (`templates/admin.html`)

**Botón en tabla de cuentas:**
- 🔍 al lado del botón Editar
- Click → `triggerDiscovery(accountName, btn)`

**JS Functions:**

```javascript
triggerDiscovery(accountName, btn)
  ├─ Deshabilita btn, cambia texto a ⏳
  ├─ POST /discover-groups
  └─ Si OK: llama pollDiscovery(runId, accountName, btn)

pollDiscovery(runId, accountName, btn)
  └─ setInterval 3s → GET /discovery/<runId>
     ├─ Si running: continúa polleando
     ├─ Si done: 
     │  ├─ Toast: "✅ 14 grupos encontrados para maria"
     │  ├─ Botón vuelve a 🔍
     │  └─ Recarga cuentas + panel de descubiertos
     └─ Si failed:
        ├─ Toast: "❌ Error: ..."
        └─ Botón vuelve a 🔍

loadDiscoveredGroupsPanel(accountName)
  ├─ GET /accounts/<name>/discovered-groups
  └─ Renderiza:
     ├─ Tabla de PENDIENTES con botón "+ Añadir"
     └─ Tabla de AÑADIDOS (readonly)

addDiscoveredGroup(accountName, groupId)
  ├─ POST .../discovered-groups/<groupId>/add
  └─ Si OK:
     ├─ Toast: "✅ Grupo añadido a publicaciones"
     ├─ Recarga panel de descubiertos
     └─ Recarga tabla de cuentas
```

---

## Flujo de usuario (end-to-end)

```
Admin panel → Tab "Cuentas"
  │
  ├─ Fila "maria" → botón 🔍
  │
  ├─ Click 🔍 → triggerDiscovery('maria', btn)
  │  ├─ btn.textContent = '⏳' (deshabilitado)
  │  └─ POST /admin/api/accounts/maria/discover-groups
  │     └─ Servidor: create_discovery_run('abc123', 'maria')
  │        → lanza _run_discovery('abc123', 'maria') en thread
  │
  ├─ Poll GET /admin/api/discovery/abc123 cada 3s
  │  ├─ [0-15s] status: "running"
  │  └─ [15-30s] status: "done", groups_found: 14
  │
  ├─ Toast: "✅ 14 grupos encontrados para maria"
  ├─ Botón: 🔍 (habilitado nuevamente)
  └─ Si está en tab de "Grupos & Tags":
     └─ Panel "Grupos descubiertos" se refresca:
        ├─ 14 grupos con estado "PENDIENTES"
        ├─ Cada uno con botón "+ Añadir"
        │
        └─ Click "+ Añadir" en "Roomies CDMX"
           ├─ POST .../discovered-groups/123456789/add
           ├─ Servidor: 
           │  ├─ Carga accounts.groups de maria
           │  ├─ Suma 123456789 a lista
           │  └─ mark_group_added_to_posting('maria', '123456789')
           └─ Toast: "✅ Grupo añadido a publicaciones"
              ├─ Grupo desaparece de tabla "PENDIENTES"
              └─ Aparece en tabla "AÑADIDOS"
```

---

## Testing checklist (PENDIENTE)

- [ ] Crear 2-3 cuentas test en Facebook
- [ ] Verificar que descubrimiento encuentra grupos reales
- [ ] Verificar que botón ⏳ funciona y desaparece
- [ ] Verificar que grupos aparecen en tabla descubiertos
- [ ] Verificar que "+ Añadir" añade grupo a accounts.groups
- [ ] Verificar que grupo aparece en tab "Configuración de cuentas"
- [ ] Verificar que se puede publicar en grupo descubierto añadido
- [ ] Verificar toast notifications (éxito y error)
- [ ] Verificar manejo de errores (FB rate limit, timeout, etc.)
- [ ] Verificar DB: tablas creadas, datos persistidos

---

## Integración con otras fases

**Fase 2 (Hardening):**
- ✅ Usa `user_data_dir` persistente (identidad por cuenta)
- ✅ Usa fingerprints únicos por cuenta (anti-detección)
- ✅ Usa pool de workers (concurrencia controlada)

**Fase 3 (Refactor):**
- 🔄 Post-Fase 3, se puede reescribir a async para paralelizar descubrimiento entre cuentas
- 🔄 Se puede agregar structured logging (cuando esté 3.3)
- 🔄 Se puede agregar snapshots DOM para tests (cuando esté 3.4)

**Fase 1.1 (Proxies):**
- ℹ️ No bloqueante. Funciona con IP única por ahora.
- 🔄 Si se implementa 1.1, cada cuenta usará su proxy → mejor enmascaramiento

---

## Notas de implementación

### ¿Por qué trigger manual + polling?
- **Polling:** Compatible con stack vanilla JS existente (sin SSE/WebSockets)
- **Manual:** Admin controla cuándo ejecutar (evita sobrecarga automática)
- **Alternativa post-Fase 3:** Async + scheduled background tasks

### ¿Por qué aprobación manual (added_to_posting)?
- **Seguridad:** Admin revisa grupos antes de que la cuenta publique
- **Flexibilidad:** Descubre 50 grupos, pero solo activa 10
- **Auditabilidad:** Log claro de qué grupos fueron aprobados y cuándo

### ¿Por qué tablas separadas (discovery_runs + discovered_groups)?
- **discovery_runs:** Historial de intentos (auditoria + debugging)
- **discovered_groups:** Catálogo persistente de grupos encontrados
- **Separación:** Si falla un run, no se pierde el catálogo anterior

---

## Commits relacionados

| Commit | Tipo | Descripción |
|--------|------|-------------|
| `feat(2.10): Auto-descubrimiento...` | Feature | Implementación completa Fase 2.10 |

---

## Próximos pasos

1. **Testing E2E** — Con cuentas reales en staging
2. **Observabilidad** — Agregar métricas (grupos/min, tasa de error)
3. **Scheduling** — Integrar en `scheduler_runner.py` para ejecución automática off-peak (Fase 2.11, si se decide)
4. **Async rewrite** — Post-Fase 3

---

**Nota:** Fase 2.10 es **funcional pero no ha sido testeada en producción aún**. Implementación sigue especificación en `plan/04_PROPUESTA_FASE_2_10.md`.
