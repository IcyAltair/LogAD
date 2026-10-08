// Minimal vanilla-JS client for the Demo Shop API (no build step, no deps).
const PAGE_SIZE = 10;
let offset = 0;
let total = 0;

const $ = (id) => document.getElementById(id);

// Escape text before inserting it into innerHTML
const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

// Thin fetch wrapper: JSON in/out, throws on non-2xx with the API error detail
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && body.detail ? JSON.stringify(body.detail) : res.statusText;
    throw new Error(`${res.status}: ${detail}`);
  }
  return body;
}

function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = isError ? "show error" : "show";
  clearTimeout(el._timer);
  el._timer = setTimeout(() => (el.className = ""), 3000);
}

// ---------------- Catalog ----------------
async function loadProducts() {
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset });
  const category = $("category").value;
  if (category) params.set("category", category);

  const tbody = $("products").querySelector("tbody");
  try {
    const data = await api(`/api/products?${params}`);
    total = data.total;
    tbody.innerHTML = "";
    for (const p of data.items) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${esc(p.id)}</td><td>${esc(p.name)}</td><td>${esc(p.category)}</td>
        <td>$${Number(p.price).toFixed(2)}</td><td>${esc(p.stock)}</td>
        <td><input type="number" class="qty" min="1" max="10" value="1"></td>
        <td><button>Buy</button></td>`;
      tr.querySelector("button").onclick = () =>
        buy(p.id, Number(tr.querySelector(".qty").value));
      tbody.appendChild(tr);
    }
    const last = Math.min(offset + PAGE_SIZE, total);
    $("page").textContent = total ? `${offset + 1}–${last} of ${total}` : "no products";
    $("prev").disabled = offset === 0;
    $("next").disabled = offset + PAGE_SIZE >= total;
  } catch (e) {
    toast(`Catalog error ${e.message}`, true);
  }
}

async function buy(productId, quantity) {
  try {
    const order = await api("/api/orders", {
      method: "POST",
      body: JSON.stringify({ product_id: productId, quantity }),
    });
    toast(`Order ${order.id} created, total $${order.total}`);
    $("orderId").value = order.id;
    loadProducts();
  } catch (e) {
    toast(`Order failed ${e.message}`, true);
  }
}

// ---------------- Orders ----------------
async function lookupOrder() {
  const id = $("orderId").value.trim();
  if (!id) return;
  try {
    const order = await api(`/api/orders/${encodeURIComponent(id)}`);
    $("orderResult").textContent = JSON.stringify(order, null, 2);
  } catch (e) {
    $("orderResult").textContent = e.message;
  }
}

// ---------------- Chaos ----------------
async function applyChaos() {
  const endpoints = $("c_endpoints").value.split(",").map((s) => s.trim()).filter(Boolean);
  const payload = {
    latency_ms: Number($("c_latency").value),
    latency_jitter_ms: Number($("c_jitter").value),
    error_rate: Number($("c_error").value),
    payload_multiplier: Number($("c_payload").value),
    duration_s: Number($("c_duration").value),
    endpoints,
  };
  try {
    renderChaos(await api("/api/chaos", { method: "POST", body: JSON.stringify(payload) }));
    toast("Chaos enabled");
  } catch (e) {
    toast(`Chaos error ${e.message}`, true);
  }
}

async function clearChaos() {
  try {
    renderChaos(await api("/api/chaos", { method: "DELETE" }));
    toast("Chaos disabled");
  } catch (e) {
    toast(`Chaos error ${e.message}`, true);
  }
}

function renderChaos(state) {
  $("chaosStatus").textContent = state.active
    ? `ACTIVE (${state.remaining_s}s left): ${JSON.stringify(state.settings)}`
    : "inactive";
}

// ---------------- Status polling ----------------
async function poll() {
  try {
    const h = await api("/api/health");
    $("health").textContent = `API ${h.status} · orders: ${h.orders}`;
    $("health").className = "badge ok";
  } catch {
    $("health").textContent = "API unavailable";
    $("health").className = "badge bad";
  }
  try { renderChaos(await api("/api/chaos")); } catch { /* ignore */ }
}

// ---------------- Wiring ----------------
$("reload").onclick = loadProducts;
$("category").onchange = () => { offset = 0; loadProducts(); };
$("prev").onclick = () => { offset = Math.max(0, offset - PAGE_SIZE); loadProducts(); };
$("next").onclick = () => { offset += PAGE_SIZE; loadProducts(); };
$("lookup").onclick = lookupOrder;
$("chaosApply").onclick = applyChaos;
$("chaosClear").onclick = clearChaos;

loadProducts();
poll();
setInterval(poll, 5000);