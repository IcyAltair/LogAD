"""
Demo Shop API: the business-logic layer of the anomaly-detection stand.

Endpoints
  GET    /api/health              liveness + basic stats
  GET    /api/products            paginated catalog (optional category filter)
  GET    /api/products/{id}       product details
  POST   /api/orders              create an order
  GET    /api/orders/{id}         order details
  GET    /api/chaos               current fault-injection state
  POST   /api/chaos               enable fault injection
  DELETE /api/chaos               disable fault injection
  GET    /metrics                 Prometheus metrics (internal, not exposed via nginx)

Handlers simulate processing time with a log-normal distribution, so latencies
in nginx logs look like a real service (long right tail), not a constant.
"""
import asyncio
import logging
import math
import random
import time
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from starlette.routing import Match

from . import metrics
from .chaos import ChaosSettings, chaos_state
from .config import settings
from .store import CATEGORIES, OutOfStockError, ProductNotFoundError, Store

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("demo-shop")

store = Store(product_count=settings.product_count, seed=settings.seed, max_orders=settings.max_orders)

# Paths that are never affected by chaos (control plane + health checks)
CHAOS_EXEMPT_PREFIXES = ("/api/chaos", "/api/health")


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
async def _restock_loop() -> None:
    """Background task that keeps stock levels stable over long experiments."""
    while True:
        await asyncio.sleep(settings.restock_interval_s)
        count = store.restock()
        if count:
            log.debug("Restocked %d products", count)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_restock_loop())
    log.info("Demo Shop API started with %d products", len(store.products))
    yield
    task.cancel()


app = FastAPI(title="Demo Shop API", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def simulate_work(median_ms: float, sigma: float = 0.5) -> None:
    """Sleep for a log-normally distributed time (median = median_ms)."""
    delay = random.lognormvariate(math.log(median_ms / 1000.0), sigma)
    await asyncio.sleep(min(delay, 5.0))


def resolve_route(request: Request) -> str:
    """Map a request to its route template to keep metric cardinality bounded."""
    for route in request.app.router.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", "unknown")
    return "unmatched"


async def maybe_inject_chaos(request: Request) -> Response | None:
    """Apply active chaos: add latency and/or short-circuit with an error response."""
    path = request.url.path
    if not path.startswith("/api/") or path.startswith(CHAOS_EXEMPT_PREFIXES):
        return None
    cfg = chaos_state.settings_for(path)
    if cfg is None:
        return None

    if cfg.latency_ms or cfg.latency_jitter_ms:
        delay_ms = cfg.latency_ms + random.uniform(-cfg.latency_jitter_ms, cfg.latency_jitter_ms)
        await asyncio.sleep(max(0.0, delay_ms) / 1000.0)

    if cfg.error_rate and random.random() < cfg.error_rate:
        code = random.choice(cfg.error_codes)
        # A generic body: the failure should look like a real outage, not a test
        return JSONResponse({"detail": "Service temporarily unavailable"}, status_code=code)
    return None


# --------------------------------------------------------------------------- #
# Middleware: metrics + chaos
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def observability_and_chaos(request: Request, call_next):
    route = resolve_route(request)
    start = time.perf_counter()
    status_code = 500  # assumed if an unhandled exception escapes
    metrics.HTTP_IN_PROGRESS.inc()
    try:
        response = await maybe_inject_chaos(request)
        if response is None:
            response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        metrics.HTTP_IN_PROGRESS.dec()
        if route != "/metrics":  # do not measure the scraper itself
            metrics.HTTP_REQUESTS.labels(request.method, route, str(status_code)).inc()
            metrics.HTTP_LATENCY.labels(request.method, route).observe(time.perf_counter() - start)


# --------------------------------------------------------------------------- #
# Business API
# --------------------------------------------------------------------------- #
class OrderIn(BaseModel):
    product_id: int = Field(..., ge=1)
    quantity: int = Field(1, ge=1, le=10)


@app.get("/api/health")
async def health():
    return {"status": "ok", "products": len(store.products), "orders": len(store.orders)}


@app.get("/api/products")
async def list_products(
    request: Request,
    category: str | None = Query(None, description=f"One of {CATEGORIES}"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    await simulate_work(median_ms=25)
    items, total = store.list_products(category, limit, offset)
    payload = [asdict(p) for p in items]

    # Chaos "heavy_payload": inflate the response body (e.g. a bug that disables pagination)
    cfg = chaos_state.settings_for(request.url.path)
    if cfg and cfg.payload_multiplier > 1:
        payload = payload * cfg.payload_multiplier

    return {"total": total, "limit": limit, "offset": offset, "items": payload}


@app.get("/api/products/{product_id}")
async def get_product(product_id: int):
    await simulate_work(median_ms=15)
    try:
        return asdict(store.get_product(product_id))
    except ProductNotFoundError:
        raise HTTPException(status_code=404, detail="Product not found")


@app.post("/api/orders", status_code=201)
async def create_order(body: OrderIn):
    await simulate_work(median_ms=60, sigma=0.6)  # "write" path is slower and noisier
    try:
        order = await store.create_order(body.product_id, body.quantity)
    except ProductNotFoundError:
        raise HTTPException(status_code=404, detail="Product not found")
    except OutOfStockError:
        raise HTTPException(status_code=409, detail="Out of stock")
    metrics.ORDERS_CREATED.labels(store.products[order.product_id].category).inc()
    return asdict(order)


@app.get("/api/orders/{order_id}")
async def get_order(order_id: str):
    await simulate_work(median_ms=10)
    order = store.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return asdict(order)


# --------------------------------------------------------------------------- #
# Chaos control plane
# --------------------------------------------------------------------------- #
def _chaos_status() -> dict:
    cfg = chaos_state.active
    return {
        "active": cfg is not None,
        "remaining_s": chaos_state.remaining_s,
        "settings": cfg.model_dump() if cfg else None,
    }


@app.get("/api/chaos")
async def get_chaos():
    return _chaos_status()


@app.post("/api/chaos")
async def set_chaos(cfg: ChaosSettings):
    chaos_state.set(cfg)
    log.warning("Chaos ENABLED: %s", cfg.model_dump())
    return _chaos_status()


@app.delete("/api/chaos")
async def clear_chaos():
    chaos_state.clear()
    log.warning("Chaos DISABLED")
    return _chaos_status()


# --------------------------------------------------------------------------- #
# Prometheus endpoint (internal network only)
# --------------------------------------------------------------------------- #
@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)