"""
Sticky session lifetime metric.

Holds a sticky session and polls it periodically until the IP changes,
recording the actual TTL. Compares against the requested sessttl.

Geekproxy default sessttl is 30 minutes. Configurable 1-120. We test
several TTLs and time out gracefully if a session outlives its expected
window by 1.5x.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from benchmark.proxy_client import GeekproxyClient, ProxyParams


@dataclass
class StickyProbe:
    timestamp: float
    ip: Optional[str]
    error: Optional[str] = None


@dataclass
class StickyLifetimeResult:
    sessid: str
    requested_ttl_minutes: int
    poll_interval_seconds: int
    started_at: float
    first_ip: Optional[str] = None
    first_change_seconds: Optional[float] = None
    final_ip: Optional[str] = None
    probes: list[StickyProbe] = field(default_factory=list)
    error: Optional[str] = None

    def summary(self) -> dict:
        return {
            "sessid": self.sessid,
            "requested_ttl_minutes": self.requested_ttl_minutes,
            "poll_interval_seconds": self.poll_interval_seconds,
            "first_ip": self.first_ip,
            "final_ip": self.final_ip,
            "first_change_seconds": self.first_change_seconds,
            "first_change_minutes": (
                round(self.first_change_seconds / 60, 2)
                if self.first_change_seconds else None
            ),
            "probe_count": len(self.probes),
            "successful_probes": sum(1 for p in self.probes if p.ip),
            "error": self.error,
        }


async def measure_sticky_lifetime(
    gp: GeekproxyClient,
    *,
    requested_ttl_minutes: int,
    poll_interval_seconds: int = 30,
    max_wall_minutes: Optional[int] = None,
    country: Optional[str] = None,
    sticky_port_offset: int = 0,
) -> StickyLifetimeResult:
    """
    Stand up one sticky session, then poll its IP every poll_interval_seconds
    until the IP changes (a rotation event) or until 1.5x of the requested
    TTL has elapsed.
    """
    sessid = GeekproxyClient.new_session_id(f"life{requested_ttl_minutes}_")
    params = ProxyParams(
        sessid=sessid,
        sessttl=requested_ttl_minutes,
        country=country,
    )
    proxy_url = gp.build_url(
        params=params,
        mode="sticky",
        sticky_port_offset=sticky_port_offset,
    )

    max_minutes = max_wall_minutes or int(requested_ttl_minutes * 1.5) + 1
    deadline = time.time() + max_minutes * 60

    result = StickyLifetimeResult(
        sessid=sessid,
        requested_ttl_minutes=requested_ttl_minutes,
        poll_interval_seconds=poll_interval_seconds,
        started_at=time.time(),
    )

    while time.time() < deadline:
        probe = await _probe_ip(proxy_url)
        result.probes.append(probe)

        if probe.ip and not result.first_ip:
            result.first_ip = probe.ip
        elif probe.ip and result.first_ip and probe.ip != result.first_ip:
            result.first_change_seconds = time.time() - result.started_at
            result.final_ip = probe.ip
            return result

        await asyncio.sleep(poll_interval_seconds)

    result.error = f"no rotation observed within {max_minutes} min"
    if result.first_ip:
        result.final_ip = result.first_ip
    return result


async def measure_sticky_lifetime_series(
    gp: GeekproxyClient,
    *,
    requested_ttl_minutes: int,
    poll_interval_seconds: int = 30,
    n_parallel: int = 5,
    country: Optional[str] = None,
) -> list[StickyLifetimeResult]:
    """
    Run N independent sticky sessions in parallel with the same TTL, so a
    single bad peer doesn't poison the metric. Caller can take the median
    or best-of-N. Returns all N results unfiltered.
    """
    tasks = [
        measure_sticky_lifetime(
            gp,
            requested_ttl_minutes=requested_ttl_minutes,
            poll_interval_seconds=poll_interval_seconds,
            country=country,
            sticky_port_offset=0,   # session bound by sessid, not by port
        )
        for i in range(n_parallel)
    ]
    return await asyncio.gather(*tasks)


async def _probe_ip(proxy_url: str) -> StickyProbe:
    ts = time.time()
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=15.0) as client_async:
            r = await client_async.get("https://api.ipify.org?format=json")
        if r.status_code == 200:
            return StickyProbe(timestamp=ts, ip=r.json().get("ip"))
        return StickyProbe(timestamp=ts, ip=None, error=f"HTTP {r.status_code}")
    except Exception as e:
        return StickyProbe(timestamp=ts, ip=None, error=f"{type(e).__name__}")
