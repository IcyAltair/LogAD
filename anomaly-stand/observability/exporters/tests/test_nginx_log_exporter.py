import json
import pytest
from prometheus_client import CollectorRegistry

import nginx_log_exporter as ex

LINE = json.dumps({"msec": "1791461400.250", "request_method": "GET", "uri": "/api/products/17",
                   "status": "200", "request_time": "0.042", "body_bytes_sent": "512"})


def test_parse_line_basic():
    r = ex.parse_line(LINE)
    assert (r.method, r.path, r.status) == ("GET", "/api/products/17", 200)
    assert r.request_time == pytest.approx(0.042) and r.body_bytes == 512
    assert r.ts == pytest.approx(1791461400.25)


def test_parse_line_iso_time_and_request_fallback():
    r = ex.parse_line(json.dumps({"time_iso8601": "2026-10-08T12:10:00+00:00",
                                  "request": "POST /api/orders HTTP/1.1", "status": 201,
                                  "request_time": "-", "body_bytes_sent": "-"}))
    assert (r.method, r.path, r.status, r.request_time, r.body_bytes) == ("POST", "/api/orders", 201, 0.0, 0.0)
    assert r.ts == pytest.approx(1791461400.0)


@pytest.mark.parametrize("bad", ["not json", "[1,2]", json.dumps({"uri": "/"}), json.dumps({"status": 200})])
def test_parse_line_rejects_garbage(bad):
    with pytest.raises((ValueError, TypeError)):
        ex.parse_line(bad)


@pytest.mark.parametrize("path,route", [
    ("/", "/"), ("/api/products?limit=2", "/api/products"), ("/api/products/42", "/api/products/:id"),
    ("/api/products/42/", "/api/products/:id"), ("/o/550e8400-e29b-41d4-a716-446655440000", "/o/:id"),
])
def test_normalize_route(path, route):
    assert ex.normalize_route(path) == route


def test_route_registry_cap_and_404():
    reg = ex.RouteRegistry(max_routes=2, known=["/"])
    assert reg.resolve("/api/products", 200) == "/api/products"
    assert reg.resolve("/random-xyz", 404) == ex.ROUTE_UNKNOWN
    assert reg.resolve("/api/new", 200) == ex.ROUTE_OTHER
    assert reg.resolve("/api/products", 404) == "/api/products"  # known route keeps its label


def test_processor_updates_metrics():
    reg = CollectorRegistry()
    proc = ex.LineProcessor(ex.LogMetrics(reg), ex.RouteRegistry(10))
    proc.process(LINE)
    proc.process("garbage")
    labels = {"method": "GET", "route": "/api/products/:id", "status": "200", "status_class": "2xx"}
    assert reg.get_sample_value("nginx_log_requests_total", labels) == 1
    assert reg.get_sample_value("nginx_log_parse_errors_total") == 1
    assert reg.get_sample_value("nginx_log_lines_total") == 2
    assert reg.get_sample_value("nginx_log_response_size_bytes_sum", {"route": "/api/products/:id"}) == 512


def test_tailer_partial_lines_truncate_and_rotate(tmp_path):
    p = tmp_path / "access.log"
    p.write_text("old\n")
    t = ex.FileTailer(str(p), start_at_end=True)
    assert t.read_lines() == []                      # history skipped
    with open(p, "a") as f:
        f.write("a\nb")                              # 'b' is partial
    assert t.read_lines() == ["a\n"]
    with open(p, "a") as f:
        f.write("c\n")
    assert t.read_lines() == ["bc\n"]
    p.write_text("x\n")                              # truncation
    assert t.read_lines() == ["x\n"] and t.reopens == 1
    p.rename(tmp_path / "access.log.1")              # rotation
    p.write_text("new\n")
    assert t.read_lines() == ["new\n"] and t.reopens == 2
