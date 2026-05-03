# Fase 3.3b — Prometheus + Grafana (Setup & Usage)

Esta guía te muestra cómo activar y usar las métricas en tiempo real del Facebook Auto-Poster.

---

## Quick Start

### 1. Activar métricas en `.env`

```bash
METRICS_ENABLED=1
```

### 2. Opción A: Ver métricas en el admin panel (sin Docker)

Abre http://localhost:5000/admin y ve a la pestaña **"Métricas"** — se actualiza cada 3 segundos con datos de la DB real.

```bash
python facebook_auto_poster/main.py
# → http://0.0.0.0:5000/admin → Tab "Métricas"
```

### 3. Opción B: Levanta Prometheus + Grafana (Docker)

```bash
# Activar en .env primero
docker-compose -f docker-compose.monitoring.yml up -d

# Prometheus: http://localhost:9090
# Grafana:    http://localhost:3000  (admin / admin)
```

---

## Métricas expuestas

Todas en el endpoint `/metrics` (formato Prometheus):

| Métrica | Tipo | Descripción |
|---------|------|-------------|
| `fb_jobs_total` | Counter | Jobs completados (done/failed/interrupted) |
| `fb_publish_total` | Counter | Publicaciones (account, result) |
| `fb_login_total` | Counter | Logins (account, result) |
| `fb_api_requests_total` | Counter | Requests HTTP (endpoint, status) |
| `fb_publish_duration_seconds` | Histogram | Duración de publicaciones (p50/p95/p99) |
| `fb_pending_jobs` | Gauge | Jobs en cola (pending+running) |
| `fb_active_accounts` | Gauge | Cuentas activas sin ban |
| `fb_banned_accounts` | Gauge | Cuentas en cooldown |

### Sin el flag `METRICS_ENABLED=1`

Todas las funciones de métricas son **no-ops** — cero overhead.

---

## Dashboard Grafana

El dashboard `FB AutoPoster` aparece automáticamente provisionado:

### Paneles incluidos

1. **Jobs completados (24h)** — pie chart por estado
2. **Jobs en cola** — gauge con advertencia si > 20
3. **Tasa de publicaciones por cuenta** — time series (5m rate)
4. **Latencia p95** — duración de publicaciones por cuenta
5. **Cuentas activas** — gauge
6. **Cuentas baneadas** — gauge

### Refresh automático

- Dashboard: cada 10 segundos
- Prometheus scrape: cada 15 segundos

---

## Troubleshooting

### "`/metrics` devuelve 404"

`METRICS_ENABLED` no está activado en `.env`, o el cambio no fue recargado.

```bash
# Verifica
grep METRICS_ENABLED facebook_auto_poster/.env

# Reinicia
python facebook_auto_poster/main.py
```

### "Prometheus no puede conectar a `host.docker.internal:5000`"

En Linux, `host.docker.internal` podría no estar disponible. Alternativas:

```yaml
# En monitoring/prometheus.yml, reemplaza:
targets: ['localhost:5000']  # si ejecutas en el host
# o
targets: ['172.17.0.1:5000']  # IP del host desde el container
```

### "Grafana muestra 'No data'"

1. Verifica que Prometheus scrapeó exitosamente:
   - Abre http://localhost:9090/targets
   - Status debe ser `UP`
2. Ejecuta un job para generar métricas:
   ```bash
   curl -X POST http://localhost:5000/post \
     -H "X-API-Key: ${OPENCLAW_API_KEY}" \
     -H "Content-Type: application/json" \
     -d '{"text":"test","accounts":["account1"]}'
   ```
3. Espera 15 segundos a que Prometheus haga el scrape siguiente.

---

## Gestión del stack Docker

```bash
# Levantar
docker-compose -f docker-compose.monitoring.yml up -d

# Ver logs
docker-compose -f docker-compose.monitoring.yml logs -f

# Parar
docker-compose -f docker-compose.monitoring.yml down

# Limpiar (borrar datos históricos)
docker-compose -f docker-compose.monitoring.yml down -v
```

---

## Personalizar el dashboard

1. Abre Grafana en http://localhost:3000
2. Abre el dashboard "FB AutoPoster"
3. Edita paneles: click en el título → "Edit"
4. Cambia queries PromQL o visualización
5. **Importante:** Si guardas cambios, NO habrá persistencia — la próxima vez que reinicies los contenedores se reestablecerá desde el JSON original.

Para hacer cambios permanentes, actualiza `monitoring/grafana/provisioning/dashboards/fb_autoposter.json` y reinicia Docker.

---

## Tab de métricas en admin panel

Accesible sin Docker — simplemente abre http://localhost:5000/admin:

### Datos mostrados

- **Gauges:** jobs pendientes, en ejecución, completados, fallidos, cuentas activas
- **Cuentas en progreso:** lista en vivo de cuentas que se están ejecutando ahora
- **Timestamp:** última actualización

### Polling automático

Se actualiza automáticamente cada 3 segundos mientras estés en el tab "Métricas".

### API endpoints usados

- `GET /admin/api/queue` — jobs_by_status, accounts_in_progress
- `GET /admin/api/accounts` — count_active

---

## Casos de uso

### Detectar cuentas baneadas

Abre Grafana → "Cuentas baneadas" gauge. Si aparece un número > 0, significa que al menos una cuenta tiene un ban activo en cooldown de 48h.

### Monitorear tasa de éxito

Panel "Tasa de publicaciones" muestra `rate(fb_publish_total[5m])` separado por `success` y `failure`. Si success cae abruptamente, investi google:

```bash
tail -50 facebook_auto_poster/logs/main.log | grep -i error
```

### Alertas automáticas (nivel avanzado)

En Prometheus (`monitoring/prometheus.yml`), puedes añadir alert rules:

```yaml
groups:
  - name: fb_alerts
    rules:
      - alert: HighFailureRate
        expr: rate(fb_publish_total{result="failure"}[5m]) > 0.3
        for: 5m
        annotations:
          summary: "Tasa de fallo > 30% en publicaciones"
```

Luego configura en Grafana http://localhost:3000/alerting para notificaciones.

---

## Limpieza

Cuando desactives métricas, no hay limpieza necesaria — el código es no-op y no genera archivos ni data de base de datos.

```bash
# Simplemente
METRICS_ENABLED=0
# (o borra la línea de .env)
```

---

## Referencia técnica

Todos los detalles de implementación están en:
- `facebook_auto_poster/metrics.py` — definición de métricas
- `facebook_auto_poster/api_server.py` — endpoint `/metrics`
- `facebook_auto_poster/facebook_poster_async.py` — calls a `metrics.inc_login()`, `metrics.inc_publish()`, etc.
- `facebook_auto_poster/templates/admin.html` — tab "Métricas" + JS de polling

La configuración de Prometheus está en `monitoring/prometheus.yml`.
El dashboard de Grafana está en `monitoring/grafana/provisioning/dashboards/fb_autoposter.json`.
