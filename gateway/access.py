"""Who may use the app: the IP allow-list, the real client address, and per-client limits."""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass


def _address(text: str):
    ip = ipaddress.ip_address(text.strip())
    # An IPv4 client can appear as ::ffff:1.2.3.4. Compare it as the IPv4 address it really is.
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


@dataclass(frozen=True)
class AllowList:
    networks: tuple
    allow_all: bool = False

    @classmethod
    def parse(cls, raw: str) -> "AllowList":
        items = [item.strip() for item in (raw or "").split(",") if item.strip()]
        if not items:
            raise ValueError("it is empty; list addresses or CIDR ranges, or write `all`")
        if any(item.lower() == "all" for item in items):
            if len(items) > 1:
                raise ValueError("`all` can't be combined with other entries")
            return cls(networks=(), allow_all=True)
        networks = []
        for item in items:
            try:
                networks.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                raise ValueError(f"{item!r} is not an IP address or CIDR range") from None
        return cls(networks=tuple(networks))

    def allows(self, client: str | None) -> bool:
        if self.allow_all:
            return True
        if not client:
            return False
        try:
            ip = _address(client)
        except ValueError:
            return False
        return any(ip in net for net in self.networks)


def real_client(peer: str | None, x_real_ip: str | None, trusted: frozenset[str]) -> str | None:
    """The address to judge a request by.

    X-Real-IP counts only when the connection comes from the host nginx, which overwrites it with
    the true address. From anyone else the header is ignored, so it can't be spoofed. A malformed
    value from the proxy gives None, which is refused unless ALLOWED_IPS=all. X-Forwarded-For is
    deliberately never read, because it can carry whatever the client sent.
    """
    if peer in trusted and x_real_ip:
        try:
            return str(_address(x_real_ip))
        except ValueError:
            return None
    return peer


class RateLimiter:
    """A token bucket per client. Memory is bounded: idle clients are dropped."""

    MAX_CLIENTS = 4096

    def __init__(self, per_minute: int, clock=time.monotonic):
        self.rate = per_minute / 60.0
        self.burst = float(per_minute)
        self.clock = clock
        self.buckets: dict[str, tuple[float, float]] = {}

    def allow(self, client: str) -> bool:
        now = self.clock()
        tokens, last = self.buckets.get(client, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        if len(self.buckets) >= self.MAX_CLIENTS and client not in self.buckets:
            self._evict(now)
        self.buckets[client] = (tokens, now)
        return allowed

    def _evict(self, now: float) -> None:
        idle = now - self.burst / self.rate
        self.buckets = {k: v for k, v in self.buckets.items() if v[1] > idle}
        if len(self.buckets) >= self.MAX_CLIENTS:
            self.buckets.clear()
