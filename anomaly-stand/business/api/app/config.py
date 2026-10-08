"""Application settings loaded from environment variables."""
import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    # Size of the seeded catalog; must match PRODUCT_COUNT of the traffic generator
    product_count: int = field(default_factory=lambda: _int("PRODUCT_COUNT", 50))
    # Seed makes the catalog identical across restarts (reproducible experiments)
    seed: int = field(default_factory=lambda: _int("SEED", 42))
    # Orders are kept in memory; oldest are evicted beyond this limit
    max_orders: int = field(default_factory=lambda: _int("MAX_ORDERS", 10_000))
    # Periodic restock prevents a slow "out of stock" drift that would look like an anomaly
    restock_interval_s: int = field(default_factory=lambda: _int("RESTOCK_INTERVAL_S", 30))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())


settings = Settings()