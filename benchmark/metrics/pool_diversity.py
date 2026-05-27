"""
Pool diversity metric.

Fires N requests through rotating proxies, collects the returned IPs, and
reports how many were unique at /32 (full IP), /24, /16. High diversity =
big pool with low repeat risk. Low diversity = small or hot pool.

Can be scoped per country to compare pool sizes geographically.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from benchmark.proxy_client import GeekproxyClient, ProxyParams


@dataclass
class PoolDiversityResult:
    country: Optional[str]
    requested: int
    successful: int
    failed: int
    elapsed_seconds: float
    ips: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def unique_ips(self) -> int:
        return len(set(self.ips))

    @property
    def unique_24(self) -> int:
        return len({_subnet(ip, 24) for ip in self.ips})

    @property
    def unique_16(self) -> int:
        return len({_subnet(ip, 16) for ip in self.ips})

    def summary(self) -> dict:
        return {
            "country": self.country,
            "requested": self.requested,
            "successful": self.successful,
            "failed": self.failed,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "unique_ips": self.unique_ips,
            "unique_24": self.unique_24,
            "unique_16": self.unique_16,
            "uniqueness_ratio_ip": round(self.unique_ips / max(self.successful, 1), 3),
            "uniqueness_ratio_24": round(self.unique_24 / max(self.successful, 1), 3),
        }


def _subnet(ip: str, prefix: int) -> str:
    try:
        net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
        return str(net.network_address)
    except ValueError:
        return ip


async def measure_pool_diversity(
    gp: GeekproxyClient,
    n_requests: int,
    *,
    country: Optional[str] = None,
    concurrency: int = 25,
    timeout: float = 20.0,
) -> PoolDiversityResult:
    """
    Rotate through n_requests independent proxy connections and count
    unique IPs. Uses port 823 (rotating). The country param can be
    used to scope to a single country.
    """
    result = PoolDiversityResult(
        country=country,
        requested=n_requests,
        successful=0,
        failed=0,
        elapsed_seconds=0.0,
    )
    sem = asyncio.Semaphore(concurrency)
    t0 = time.perf_counter()

    async def one() -> None:
        params = ProxyParams(country=country) if country else ProxyParams()
        proxy_url = gp.build_url(params=params, mode="rotating")
        async with sem:
            try:
                async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client_async:
                    r = await client_async.get("https://api.ipify.org?format=json")
                if r.status_code == 200:
                    ip = r.json().get("ip")
                    if ip:
                        result.ips.append(ip)
                        result.successful += 1
                        return
                result.failed += 1
                result.errors.append(f"HTTP {r.status_code}")
            except Exception as e:
                result.failed += 1
                result.errors.append(f"{type(e).__name__}")

    await asyncio.gather(*[one() for _ in range(n_requests)])
    result.elapsed_seconds = time.perf_counter() - t0
    return result
