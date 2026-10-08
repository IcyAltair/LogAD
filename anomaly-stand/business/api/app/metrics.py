"""
Prometheus metrics of the business service (scraped by Prometheus in step 2).

Label cardinality is kept low: `route` is the route *template*
(e.g. /api/products/{product_id}), never the raw URL.
"""
from prometheus_client import Counter, Gauge, Histogram

from .chaos import chaos_state

HTTP_REQUESTS = Counter(
    "app_http_requests_total",
    "Total HTTP requests processed by the app",
    ["method", "route", "status"],
)

HTTP_LATENCY = Histogram(
    "app_http_request_duration_seconds",
    "Request processing time inside the app",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

HTTP_IN_PROGRESS = Gauge("app_http_requests_in_progress", "Requests currently being processed")

ORDERS_CREATED = Counter("app_orders_created_total", "Successfully created orders", ["category"])

# Experiment annotation only (helps to eyeball injected faults on dashboards).
# It must NOT be used as an ML feature — that would leak the labels.
CHAOS_ACTIVE = Gauge("app_chaos_active", "1 if fault injection is currently active")
CHAOS_ACTIVE.set_function(lambda: 1.0 if chaos_state.active else 0.0)