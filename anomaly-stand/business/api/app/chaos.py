"""
Chaos / fault-injection state.

Lets experiments degrade the service in controlled, *operational* ways
(latency, 5xx errors, oversized payloads). Every chaos config has a TTL, so a
crashed controller can never leave the service broken forever.
"""
import time

from pydantic import BaseModel, Field, field_validator


class ChaosSettings(BaseModel):
    latency_ms: int = Field(0, ge=0, le=30_000, description="Extra latency added to each request")
    latency_jitter_ms: int = Field(0, ge=0, le=10_000, description="Uniform +/- jitter around latency_ms")
    error_rate: float = Field(0.0, ge=0.0, le=1.0, description="Probability of an injected error response")
    error_codes: list[int] = Field(default_factory=lambda: [500, 503])
    payload_multiplier: int = Field(1, ge=1, le=200, description="Inflates catalog list responses")
    endpoints: list[str] = Field(default_factory=list, description="Path prefixes; empty = all /api routes")
    duration_s: int = Field(300, ge=1, le=3600, description="Auto-disable after N seconds")

    @field_validator("error_codes")
    @classmethod
    def _valid_codes(cls, codes: list[int]) -> list[int]:
        if not codes or any(c < 400 or c > 599 for c in codes):
            raise ValueError("error_codes must be a non-empty list of HTTP codes 400..599")
        return codes


class ChaosState:
    def __init__(self) -> None:
        self._settings: ChaosSettings | None = None
        self._expires_at: float = 0.0

    def set(self, settings: ChaosSettings) -> None:
        self._settings = settings
        self._expires_at = time.monotonic() + settings.duration_s

    def clear(self) -> None:
        self._settings = None
        self._expires_at = 0.0

    @property
    def active(self) -> ChaosSettings | None:
        """Current settings, or None if not set / expired (lazy expiry)."""
        if self._settings is not None and time.monotonic() >= self._expires_at:
            self.clear()
        return self._settings

    @property
    def remaining_s(self) -> int:
        return max(0, int(self._expires_at - time.monotonic())) if self.active else 0

    def settings_for(self, path: str) -> ChaosSettings | None:
        """Return active settings if they apply to the given request path."""
        settings = self.active
        if settings is None:
            return None
        if settings.endpoints and not any(path.startswith(p) for p in settings.endpoints):
            return None
        return settings


# Process-wide singleton (uvicorn runs with a single worker)
chaos_state = ChaosState()