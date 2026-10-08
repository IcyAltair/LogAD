"""
Synthetic traffic generator for the Demo Shop (business layer).

1. Normal traffic: user actions sent through nginx. The request rate follows a
   compressed diurnal cycle (one simulated "day" = DAY_PERIOD_S seconds) with
   Poisson arrivals and multiplicative noise, so the model sees realistic
   seasonality and noise instead of a flat line. A small share of "natural"
   errors (404 for missing products, 422 for bad input) is part of normal traffic.

2. Anomaly scenarios (operational, NOT security-related) start after the
   warm-up period, which provides clean training data:
     traffic_spike        sudden load increase (flash sale, client retry storm)
     latency_degradation  slow catalog backend
     error_burst          backend returns 5xx
     not_found_storm      broken client / bad deploy requesting missing resources
     heavy_payload        abnormally large responses (pagination bug)

3. Ground truth: every scenario is appended to a JSONL file with its time window
   and parameters. It is used to validate the Isolation Forest (step 3).

Chaos is configured via CHAOS_URL (the app directly), bypassing nginx,
so control requests never pollute the nginx logs used for ML.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


TARGET_URL = os.getenv("TARGET_URL", "http://nginx")
CHAOS_URL = os.getenv("CHAOS_URL", "http://app:8000")
PRODUCT_COUNT = int(os.getenv("PRODUCT_COUNT", "50"))

BASE_RPS = _env_float("BASE_RPS", 8)
DAY_PERIOD_S = _env_float("DAY_PERIOD_S", 3600)
DIURNAL_AMPLITUDE = _env_float("DIURNAL_AMPLITUDE", 0.6)
RATE_NOISE = _env_float("RATE_NOISE", 0.15)

WARMUP_S = _env_float("WARMUP_S", 1800)
ANOMALIES_ENABLED = os.getenv("ANOMALIES_ENABLED", "true").lower() in ("1", "true", "yes")
ANOMALY_MIN_GAP_S = _env_float("ANOMALY_MIN_GAP_S", 300)
ANOMALY_MAX_GAP_S = _env_float("ANOMALY_MAX_GAP_S", 900)
SCENARIO_MIN_DURATION_S = int(os.getenv("SCENARIO_MIN_DURATION_S", "90"))
SCENARIO_MAX_DURATION_S = int(os.getenv("SCENARIO_MAX_DURATION_S", "240"))
SCENARIOS = [s.strip() for s in os.getenv(
    "SCENARIOS", "traffic_spike,latency_degradation,error_burst,not_found_storm,heavy_payload"
).split(",") if s.strip()]

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "300"))
GROUND_TRUTH_FILE = os.getenv("GROUND_TRUTH_FILE", "/data/ground_truth/anomalies.jsonl")
STATS_INTERVAL_S = 30

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("traffic-generator")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
    "DemoShopApp/2.3.1 (Android 14; Pixel 8)",
]

# Synthetic client IPs from RFC 5737 documentation ranges (sent as X-Forwarded-For)
CLIENT_IPS = [f"198.51.100.{i}" for i in range(1, 255)] + [f"203.0.113.{i}" for i in range(1, 255)]


# --------------------------------------------------------------------------- #
# Math helpers
# --------------------------------------------------------------------------- #
def diurnal_factor(now: float) -> float:
    """Smooth day/night multiplier in [1-A, 1+A]; minimum at the start of the period."""
    phase = 2 * math.pi * (now % DAY_PERIOD_S) / DAY_PERIOD_S
    return 1.0 + DIURNAL_AMPLITUDE * math.sin(phase - math.pi / 2)


def poisson(lam: float) -> int:
    """Poisson sample: Knuth for small lambda, normal approximation for large."""
    if lam <= 0:
        return 0
    if lam > 50:
        return max(0, int(round(random.gauss(lam, math.sqrt(lam)))))
    threshold, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= random.random()
        if p <= threshold:
            return k
        k += 1


# --------------------------------------------------------------------------- #
# Shop client: one method = one user action
# --------------------------------------------------------------------------- #
class ShopClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.recent_orders: deque[str] = deque(maxlen=500)
        self.stats: Counter[str] = Counter()

    @staticmethod
    def identity() -> dict[str, str]:
        """A random 'user': consistent UA + client IP for all requests of one action."""
        return {"User-Agent": random.choice(USER_AGENTS), "X-Forwarded-For": random.choice(CLIENT_IPS)}

    async def _request(self, method: str, path: str, headers: dict, **kwargs) -> httpx.Response | None:
        try:
            resp = await self.http.request(method, path, headers=headers, **kwargs)
            self.stats[f"{resp.status_code // 100}xx"] += 1
            return resp
        except httpx.HTTPError as exc:  # timeouts, connection resets, ...
            self.stats["client_error"] += 1
            log.debug("%s %s failed: %r", method, path, exc)
            return None

    # ---- normal behaviour ----
    async def page_load(self) -> None:
        """Opening the UI: HTML + assets (as a browser without cache would)."""
        h = self.identity()
        await self._request("GET", "/", h)
        await asyncio.gather(
            self._request("GET", "/static/app.js", {**h, "Referer": f"{TARGET_URL}/"}),
            self._request("GET", "/static/style.css", {**h, "Referer": f"{TARGET_URL}/"}),
        )

    async def browse_catalog(self) -> None:
        params = {"limit": random.choice([10, 10, 20, 50]), "offset": random.choice([0, 0, 0, 10, 20])}
        if random.random() < 0.5:
            params["category"] = random.choice(["books", "electronics", "home", "toys", "sport"])
        await self._request("GET", "/api/products", self.identity(), params=params)

    async def view_product(self) -> None:
        # ~2% natural 404s: stale links to recently removed products
        if random.random() < 0.02:
            pid = random.randint(PRODUCT_COUNT + 1, PRODUCT_COUNT + 10)
        else:
            pid = random.randint(1, PRODUCT_COUNT)
        await self._request("GET", f"/api/products/{pid}", self.identity())

    async def place_order(self) -> None:
        # ~3% natural client validation errors (422)
        quantity = 0 if random.random() < 0.03 else random.choices([1, 2, 3], weights=[70, 20, 10])[0]
        body = {"product_id": random.randint(1, PRODUCT_COUNT), "quantity": quantity}
        resp = await self._request("POST", "/api/orders", self.identity(), json=body)
        if resp is not None and resp.status_code == 201:
            self.recent_orders.append(resp.json()["id"])

    async def check_order(self) -> None:
        if not self.recent_orders:
            return await self.browse_catalog()
        order_id = random.choice(self.recent_orders)
        await self._request("GET", f"/api/orders/{order_id}", self.identity())

    # ---- anomalous behaviour ----
    async def broken_client(self) -> None:
        """Misbehaving client after a bad release: requests non-existent resources."""
        h = self.identity()
        if random.random() < 0.7:
            await self._request("GET", f"/api/products/{random.randint(1000, 99999)}", h)
        else:
            await self._request("GET", random.choice(["/static/app.v2.js", "/static/legacy.css", "/catalog"]), h)


# --------------------------------------------------------------------------- #
# Anomaly scenarios
# --------------------------------------------------------------------------- #
@dataclass
class ScenarioEffect:
    """How an active scenario modifies the client-side traffic."""
    rate_multiplier: float = 1.0
    extra_rps: float = 0.0
    extra_action: Callable[[], Awaitable[None]] | None = None


class ScenarioRunner:
    def __init__(self, chaos_http: httpx.AsyncClient, shop: ShopClient) -> None:
        self.chaos_http = chaos_http
        self.shop = shop
        self.effect = ScenarioEffect()
        self.active_name: str | None = None
        os.makedirs(os.path.dirname(GROUND_TRUTH_FILE), exist_ok=True)

    async def set_chaos(self, cfg: dict) -> None:
        resp = await self.chaos_http.post("/api/chaos", json=cfg)
        resp.raise_for_status()

    async def clear_chaos(self) -> None:
        try:
            await self.chaos_http.delete("/api/chaos")
        except httpx.HTTPError as exc:
            log.warning("Failed to clear chaos (TTL will expire it): %r", exc)

    def _write_ground_truth(self, record: dict) -> None:
        with open(GROUND_TRUTH_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _build(self, name: str) -> tuple[ScenarioEffect, dict | None, dict]:
        """Return (client effect, server chaos config, params for ground truth)."""
        if name == "traffic_spike":
            mult = round(random.uniform(4, 8), 1)
            return ScenarioEffect(rate_multiplier=mult), None, {"rate_multiplier": mult}
        if name == "latency_degradation":
            cfg = {"latency_ms": random.choice([800, 1500, 2500]), "latency_jitter_ms": 400,
                   "endpoints": ["/api/products"]}
            return ScenarioEffect(), cfg, cfg
        if name == "error_burst":
            cfg = {"error_rate": round(random.uniform(0.2, 0.5), 2), "error_codes": [500, 503]}
            return ScenarioEffect(), cfg, cfg
        if name == "not_found_storm":
            rps = round(random.uniform(10, 25), 1)
            return ScenarioEffect(extra_rps=rps, extra_action=self.shop.broken_client), None, {"extra_rps": rps}
        if name == "heavy_payload":
            cfg = {"payload_multiplier": random.choice([20, 50, 100]), "endpoints": ["/api/products"]}
            return ScenarioEffect(), cfg, cfg
        raise ValueError(f"Unknown scenario: {name}")

    async def run(self, name: str) -> None:
        duration = random.randint(SCENARIO_MIN_DURATION_S, SCENARIO_MAX_DURATION_S)
        effect, chaos_cfg, params = self._build(name)

        start = time.time()
        if chaos_cfg:
            # Server-side TTL = scenario duration: safe even if the generator dies
            await self.set_chaos({**chaos_cfg, "duration_s": duration})
        self.effect, self.active_name = effect, name

        end = start + duration
        self._write_ground_truth({
            "scenario": name,
            "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
            "start_ts": round(start, 3),
            "end_ts": round(end, 3),
            "duration_s": duration,
            "params": params,
        })
        log.warning("ANOMALY START %s for %ds params=%s", name, duration, params)
        try:
            await asyncio.sleep(duration)
        finally:
            self.effect, self.active_name = ScenarioEffect(), None
            if chaos_cfg:
                await self.clear_chaos()
            log.warning("ANOMALY END %s", name)

    async def run_forever(self) -> None:
        if not ANOMALIES_ENABLED or not SCENARIOS:
            log.info("Anomaly scenarios disabled: generating normal traffic only")
            return
        log.info("Warm-up: %ds of clean traffic before the first anomaly", WARMUP_S)
        await asyncio.sleep(WARMUP_S)
        while True:
            await asyncio.sleep(random.uniform(ANOMALY_MIN_GAP_S, ANOMALY_MAX_GAP_S))
            try:
                await self.run(random.choice(SCENARIOS))
            except Exception:  # never let a failed scenario stop the generator
                log.exception("Scenario failed")


# --------------------------------------------------------------------------- #
# Main loops
# --------------------------------------------------------------------------- #
async def traffic_loop(shop: ShopClient, runner: ScenarioRunner) -> None:
    """Every second: sample N ~ Poisson(rate) actions, spread them within the second."""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks: set[asyncio.Task] = set()
    actions = [shop.page_load, shop.browse_catalog, shop.view_product, shop.place_order, shop.check_order]
    weights = [0.10, 0.35, 0.30, 0.15, 0.10]

    def fire(action: Callable[[], Awaitable[None]]) -> None:
        async def _run() -> None:
            await asyncio.sleep(random.random())
            if sem.locked():  # back-pressure: drop instead of queueing unbounded tasks
                shop.stats["dropped"] += 1
                return
            async with sem:
                try:
                    await action()
                except Exception as exc:
                    log.debug("Action failed: %r", exc)

        task = asyncio.create_task(_run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    while True:
        tick = time.monotonic()
        effect = runner.effect
        rate = (BASE_RPS * diurnal_factor(time.time()) * effect.rate_multiplier
                * random.uniform(1 - RATE_NOISE, 1 + RATE_NOISE))
        for _ in range(poisson(rate)):
            fire(random.choices(actions, weights=weights)[0])
        if effect.extra_action is not None:
            for _ in range(poisson(effect.extra_rps)):
                fire(effect.extra_action)
        await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - tick)))


async def stats_loop(shop: ShopClient, runner: ScenarioRunner) -> None:
    while True:
        await asyncio.sleep(STATS_INTERVAL_S)
        total = sum(v for k, v in shop.stats.items() if k.endswith("xx"))
        log.info("last %ds: %.1f rps, %s, diurnal=%.2f, scenario=%s",
                 STATS_INTERVAL_S, total / STATS_INTERVAL_S, dict(shop.stats),
                 diurnal_factor(time.time()), runner.active_name or "-")
        shop.stats.clear()


async def wait_for_target(http: httpx.AsyncClient) -> None:
    while True:
        try:
            if (await http.get("/healthz")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        log.info("Waiting for nginx at %s ...", TARGET_URL)
        await asyncio.sleep(2)


async def main() -> None:
    limits = httpx.Limits(max_connections=MAX_CONCURRENCY, max_keepalive_connections=50)
    async with httpx.AsyncClient(base_url=TARGET_URL, timeout=15.0, limits=limits) as http, \
               httpx.AsyncClient(base_url=CHAOS_URL, timeout=5.0) as chaos_http:
        await wait_for_target(http)
        shop = ShopClient(http)
        runner = ScenarioRunner(chaos_http, shop)
        await runner.clear_chaos()  # reset leftovers from a previous run
        log.info("Generating traffic: base=%.1f rps, day=%ds, warmup=%ds, scenarios=%s",
                 BASE_RPS, DAY_PERIOD_S, WARMUP_S, SCENARIOS)
        await asyncio.gather(traffic_loop(shop, runner), runner.run_forever(), stats_loop(shop, runner))


if __name__ == "__main__":
    asyncio.run(main())