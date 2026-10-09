"""
Operational anomaly detection on nginx JSON access logs with Isolation Forest.

  config      env-driven settings shared by every entry point
  logs        log parsing, route normalisation, file tailing
  features    window aggregation + feature engineering (SAME code offline & online)
  labels      ground truth (step-1 traffic-generator) -> window labels
  model       Isolation Forest pipeline + persisted model bundle
  evaluation  window / event metrics, k-of-n alert debounce
  pipeline    CLI: prepare -> train -> test (run = all three)
  detector    online scoring service (FastAPI + Prometheus /metrics)
"""
__version__ = "2.0.0"