"""
In-memory domain storage: products and orders.

Persistence is intentionally absent: the stand is restarted often and business
state is irrelevant for log-based anomaly detection.
"""
import asyncio
import random
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

CATEGORIES = ["books", "electronics", "home", "toys", "sport"]


class ProductNotFoundError(Exception):
    """Requested product does not exist."""


class OutOfStockError(Exception):
    """Not enough items in stock to fulfil the order."""


@dataclass
class Product:
    id: int
    name: str
    category: str
    price: float
    stock: int


@dataclass
class Order:
    id: str
    product_id: int
    quantity: int
    total: float
    status: str
    created_at: str


class Store:
    def __init__(self, product_count: int, seed: int, max_orders: int) -> None:
        rnd = random.Random(seed)  # local RNG -> deterministic catalog
        self.products: dict[int, Product] = {}
        for pid in range(1, product_count + 1):
            category = rnd.choice(CATEGORIES)
            self.products[pid] = Product(
                id=pid,
                name=f"{category.title()} item #{pid}",
                category=category,
                price=round(rnd.uniform(5, 500), 2),
                stock=rnd.randint(100, 500),
            )
        # OrderedDict lets us evict the oldest orders in O(1)
        self.orders: "OrderedDict[str, Order]" = OrderedDict()
        self.max_orders = max_orders
        self._lock = asyncio.Lock()

    # ---------- products ----------
    def list_products(self, category: str | None, limit: int, offset: int) -> tuple[list[Product], int]:
        items = [p for p in self.products.values() if category is None or p.category == category]
        return items[offset: offset + limit], len(items)

    def get_product(self, product_id: int) -> Product:
        product = self.products.get(product_id)
        if product is None:
            raise ProductNotFoundError(product_id)
        return product

    def restock(self, threshold: int = 20, amount: int = 200) -> int:
        """Refill low-stock products. Returns number of restocked products."""
        restocked = 0
        for product in self.products.values():
            if product.stock < threshold:
                product.stock += amount
                restocked += 1
        return restocked

    # ---------- orders ----------
    async def create_order(self, product_id: int, quantity: int) -> Order:
        # The lock is not strictly needed in single-threaded asyncio (no awaits inside),
        # but it documents the critical section and protects against future changes.
        async with self._lock:
            product = self.get_product(product_id)
            if product.stock < quantity:
                raise OutOfStockError(product_id)
            product.stock -= quantity
            order = Order(
                id=uuid.uuid4().hex[:12],
                product_id=product_id,
                quantity=quantity,
                total=round(product.price * quantity, 2),
                status="created",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self.orders[order.id] = order
            if len(self.orders) > self.max_orders:
                self.orders.popitem(last=False)
            return order

    def get_order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)