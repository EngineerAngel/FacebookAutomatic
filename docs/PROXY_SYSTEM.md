# Sistema de Proxies SIM — Documentación Técnica

## Cambios implementados (2026-04-26)

### 1. Asignación dinámica LRU de proxies

**Antes:** Las cuentas se asignaban manualmente a los nodos proxy. Si una cuenta sin proxy necesitaba publicar, fallaba.

**Ahora:** El sistema asigna proxies dinámicamente cuando una cuenta lo necesita:

```
resolve_proxy(account_name)
  ├─ ¿Tiene asignación y nodo online?
  │   └─ retornar proxy ✓
  ├─ ¿Sin asignación?
  │   ├─ ¿Hay nodo con espacio libre?
  │   │   └─ asignar directo
  │   └─ ¿Todos llenos?
  │       └─ expulsar cuenta LRU, asignar al solicitante
  └─ Actualizar last_used_at para mantener LRU fresco
```

**Ventaja clave:** Si agregas un segundo teléfono, las cuentas se redistribuyen automáticamente sin intervención manual.

### 2. Control de capacidad por nodo

```python
MAX_ACCOUNTS_PER_NODE = 10  # configurable
```

Cada teléfono puede tener máximo 10 cuentas asignadas (evita sobrecarga de la SIM).

### 3. Rotación LRU (Least Recently Used)

Cuando todos los nodos están llenos y llega una cuenta nueva que necesita publicar:

1. Se busca la cuenta con `last_used_at` más antiguo (la que lleva más tiempo sin publicar)
2. Se desasigna esa cuenta del nodo
3. Se asigna el nodo liberado a la cuenta que acaba de pedir publicar
4. Se loggea: `"ROTACIÓN: 'cuenta_vieja' expulsada de nodo → entra 'cuenta_nueva'"`

**Ejemplo real:**
```
[Proxy] ROTACIÓN: 'anna_martit' expulsada de phone1_sim (último uso: nunca) → entra 'nueva_cuenta'
```

### 4. Mejoras en setup_phone_proxy.sh

#### Detección automática de protocolo
```bash
./setup_phone_proxy.sh --add
```
Prueba automáticamente SOCKS5, HTTP y SOCKS4 (antes solo SOCKS5).

#### Nuevos comandos
| Comando | Función |
|---------|---------|
| `--info NODE_ID` | Ver detalles completos de un nodo |
| `--edit NODE_ID` | Cambiar servidor, etiqueta, notas |
| `--remove NODE_ID` | Eliminar nodo (con confirmación) |
| `--fix NODE_ID` | Re-detectar IP/puerto (útil si el teléfono cambió de IP) |
| `--assign NODE_ID CUENTA` | Asignar proxy a cuenta manualmente |
| `--unassign CUENTA` | Quitar proxy de una cuenta |

#### Usa Python del venv
```bash
_pick_python() {
    # Detecta automáticamente el venv con PySocks
    # Fallback al sistema Python si no encuentra venv
}
```
Soluciona el error `Missing dependencies for SOCKS support`.

### 5. Cambios en job_store.py

#### Nueva columna en tabla `account_proxy_assignment`
```sql
ALTER TABLE account_proxy_assignment ADD COLUMN last_used_at TEXT
```

Almacena cuándo se usó último el proxy de cada cuenta (ISO 8601).

#### Nuevas funciones

```python
touch_proxy_assignment(account_name)
    # Actualiza last_used_at = ahora
    # Se llama automáticamente en resolve_proxy() cuando retorna exitosamente

count_accounts_for_node(node_id) → int
    # Número de cuentas con primary_node = node_id
    # Usado para verificar si hay espacio libre

get_lru_account_for_node(node_id) → dict
    # Retorna la cuenta que lleva más tiempo sin usar el proxy
    # ORDER BY last_used_at ASC NULLS FIRST (nunca usadas primero)
    # Candidata a expulsión cuando llega cuenta nueva
```

### 6. Cambios en proxy_manager.py

#### Nueva estrategia de resolución

```python
def resolve_proxy(account_name, force_refresh=False) -> dict | None:
    """
    Retorna proxy, asignando dinámicamente si es necesario.
    
    Prioridad:
    1. Cache válido (30s TTL)
    2. Nodo primario online
    3. Nodo secundario (fallback manual)
    4. Asignación dinámica (slot libre o rotación LRU)
    5. Fallback emergencia (cualquier nodo online)
    """
```

#### Nuevas funciones internas

```python
_assign_to_free_slot(account_name, groups)
    # Busca nodo con capacidad libre
    # Evita solapamiento de grupos (cuentas en mismos grupos → proxies distintos)

_evict_lru_and_assign(account_name, groups)
    # Expulsa la cuenta con last_used_at más antiguo
    # Asigna el nodo liberado al solicitante

_ensure_assigned(account_name)
    # Garantiza que la cuenta tenga proxy
    # Protected con lock global (thread-safe)
```

### 7. Requierement.txt

```
PySocks~=1.7       # soporte SOCKS5 para requests (proxies SIM)
```

Agregado para que `proxy_manager._check_node()` pueda validar proxies SOCKS5.

---

## Flujo completo: Publicar una cuenta sin proxy asignado

```
Usuario pide: publishar con 'cuenta_nueva' (sin proxy)
    ↓
resolve_proxy('cuenta_nueva', force_refresh=False)
    ├─ ¿Tiene asignación en DB?  NO
    │   ↓
    ├─ _ensure_assigned('cuenta_nueva')
    │   ├─ Lock global (evita race conditions)
    │   ├─ _assign_to_free_slot()
    │   │   ├─ ¿Hay nodo online con < 10 cuentas?
    │   │   │   └─ SÍ → asignar y retornar
    │   │   └─ NO → continue
    │   │
    │   └─ _evict_lru_and_assign()
    │       ├─ Buscar nodo cuya cuenta LRU es más antigua
    │       ├─ delete_proxy_assignment(lru_account)
    │       ├─ Log: "ROTACIÓN: 'lru_account' expulsada → entra 'cuenta_nueva'"
    │       └─ set_proxy_assignment('cuenta_nueva', node_id)
    │
    ├─ node = job_store.get_proxy_node(node_id)
    ├─ touch_proxy_assignment('cuenta_nueva')  # actualizar last_used_at
    └─ return {'server': 'socks5://10.142.156.188:1080'}

FacebookPoster abre Chromium con --proxy socks5://...
    └─ publicación procede normalmente
```

---

## Monitoreo

### Ver estado actual
```bash
./setup_phone_proxy.sh --status
# ✓ phone1_sim  [socks5://10.142.156.188:1080]  OK  IP: 189.203.1.244
```

### Ver asignaciones y último uso
```bash
./setup_phone_proxy.sh --list
# Tel1        ONLINE  socks5://...  IP: 189.203.1.244  Cuentas: 0
# phone1_sim  ONLINE  socks5://...  IP: 189.203.1.244  Cuentas: 5
#   └─ andrea_zalazar      (last_used: 2026-04-26 22:54)
#   └─ anna_martit          (last_used: —)
#   ...
```

### Ver LRU de un nodo
```bash
./setup_phone_proxy.sh --info phone1_sim
# LRU (candidata a expulsión): anna_martit (last_used: nunca)
```

---

## Parámetros ajustables

```python
# proxy_manager.py
CHECK_INTERVAL_S = 120      # health check cada 2 minutos
FAIL_THRESHOLD = 3          # fallos → offline
MAX_ACCOUNTS_PER_NODE = 10  # capacidad máxima por nodo

# proxy_manager._proxy_cache
_PROXY_CACHE_TTL_S = 30     # cache de resoluciones (30s)
```

---

## Ventajas del nuevo sistema

| Escenario | Antes | Ahora |
|-----------|-------|-------|
| Agregar 2do teléfono | Reconfigurar cuentas manualmente | Redistribución automática |
| Cuenta sin proxy necesita publicar | Falla con error | Se asigna dinámicamente |
| Pool de proxies lleno | No se pueden agregar más cuentas | Rotación LRU automática |
| Editar un nodo | Borrar y recrear | `--edit NODE_ID` |
| Diagnosticar fallo | Error genérico | Diagnóstico detallado por protocolo |
| Usar Python del sistema sin PySocks | Error SOCKS5 | Auto-detecta y usa venv |

---

## Próximos pasos opcionales

1. **Configurar `execution_mode = "sequential"`** en `config.py` si tienes mala señal SIM
2. **Agregar segundo teléfono** — automáticamente se redistribuirán cuentas
3. **Ajustar `MAX_ACCOUNTS_PER_NODE`** si las SIMs aguantan más/menos carga
4. **Monitorear logs** (`logs/main.log`) para ver rotaciones LRU en acción:
   ```
   [Proxy] ROTACIÓN: 'cuenta_vieja' expulsada de phone1_sim → entra 'cuenta_nueva'
   ```

---

## Troubleshooting rápido

```bash
# "Missing dependencies for SOCKS support"
./setup_phone_proxy.sh --status
# → verifica que esté usando /home/angel/Proyectos/.venv/bin/python3

# Teléfono desconectado
./setup_phone_proxy.sh --status
# → si muestra OFFLINE, reconectar cable USB y ejecutar:
./setup_phone_proxy.sh --fix phone1_sim

# Ver todas las rotaciones LRU ocurridas
tail -f facebook_auto_poster/logs/main.log | grep "ROTACIÓN"
```
