"""
metrics.py — Prometheus metrics para Facebook Auto-Poster (Fase 3.3b).

Activar con METRICS_ENABLED=1 en .env.
Cuando metrics_enabled=False, todas las funciones son no-ops para cero overhead.
"""
from __future__ import annotations

from config import CONFIG

_ENABLED = CONFIG.get("metrics_enabled", False)

if _ENABLED:
    from prometheus_client import Counter, Histogram, REGISTRY
    from prometheus_client.core import GaugeMetricFamily
    import job_store

    # --- Counters (se incrementan solo hacia adelante) ---
    jobs_total = Counter(
        "fb_jobs_total", "Total jobs completados", ["status"]
    )
    publish_total = Counter(
        "fb_publish_total", "Total intentos de publicación", ["account", "result"]
    )
    login_total = Counter(
        "fb_login_total", "Total intentos de login", ["account", "result"]
    )
    api_requests_total = Counter(
        "fb_api_requests_total", "Requests a endpoints OpenClaw", ["endpoint", "http_status"]
    )

    # --- Histogram ---
    publish_duration = Histogram(
        "fb_publish_duration_seconds",
        "Duración de publicación por cuenta",
        ["account"],
        buckets=[5, 15, 30, 60, 120, 300],
    )

    # --- Gauges via Collector (consultan DB en cada scrape) ---
    class _DBCollector:
        """Collector que consulta job_store en cada scrape de Prometheus."""

        def collect(self):
            counts = job_store.count_jobs_by_status()
            pending = counts.get("pending", 0) + counts.get("running", 0)
            g = GaugeMetricFamily("fb_pending_jobs", "Jobs pendientes o corriendo")
            g.add_metric([], pending)
            yield g

            active = job_store.count_active_accounts()
            g2 = GaugeMetricFamily("fb_active_accounts", "Cuentas activas sin ban")
            g2.add_metric([], active)
            yield g2

            bans = len(job_store.list_active_bans())
            g3 = GaugeMetricFamily("fb_banned_accounts", "Cuentas en cooldown de ban")
            g3.add_metric([], bans)
            yield g3

    REGISTRY.register(_DBCollector())


# --- Helpers públicos (no-ops cuando metrics_enabled=False) ---

def inc_job(status: str) -> None:
    if _ENABLED:
        jobs_total.labels(status=status).inc()


def inc_publish(account: str, success: bool) -> None:
    if _ENABLED:
        publish_total.labels(account=account, result="success" if success else "failure").inc()


def inc_login(account: str, success: bool) -> None:
    if _ENABLED:
        login_total.labels(account=account, result="success" if success else "failure").inc()


def inc_api_request(endpoint: str, http_status: int) -> None:
    if _ENABLED:
        api_requests_total.labels(endpoint=endpoint, http_status=str(http_status)).inc()


def observe_publish_duration(account: str, seconds: float) -> None:
    if _ENABLED:
        publish_duration.labels(account=account).observe(seconds)
