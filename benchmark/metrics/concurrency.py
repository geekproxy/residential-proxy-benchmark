"""
Concurrency stress metric.

Geekproxy docs cap concurrent threads at 2000 (407 THREADS_EXHAUSTED).
This metric walks up the concurrency ladder (10, 50, 200, 500, 1000,
2000, 2500) and reports for each level:
  - success rate
  - p50 / p95 / p99 latency
  - thread-exhausted error rate
  - timeout rate

Lets us plot a real degradation curve instead of trusting "unlimited concurrent".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from benchmark.proxy_client import GeekproxyClient, ProxyParams


@dataclass
class ConcurrencyLevelResult:
    concurrency: int
    n_requests: int
    successes: int = 0
    timeouts: int = 0
    thread_exhausted: int = 0
    other_http_errors: int = 0
    other_exceptions: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / max(self.n_requests, 1)

    def latency(self, p: float) -> Optional[float]:
        if not self.latencies_ms:
            return None
        s = sorted(self.latencies_ms)
        idx = min(len(s) - 1, int(p * (len(s) - 1)))
        return round(s[idx], 1)

    def summary(self) -> dict:
        return {
            "concurrency": self.concurrency,
            "n_requests": self.n_requests,
            "successes": self.successes,
            "success_rate": round(self.success_rate, 3),
            "timeouts": self.timeouts,
            "thread_exhausted": self.thread_exhausted,
            "other_http_errors": self.other_http_errors,
            "other_exceptions": self.other_exceptions,
            "p50_ms": self.latency(0.5),
            "p95_ms": self.latency(0.95),
            "p99_ms": self.latency(0.99),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "throughput_rps": round(self.n_requests / max(self.elapsed_seconds, 0.001), 2),
        }


async def measure_concurrency_level(
    gp: GeekproxyClient,
    concurrency: int,
    *,
    n_requests: Optional[int] = None,
    target_url: str = "https://api.ipify.org?format=json",
    timeout: float = 30.0,
    country: Optional[str] = None,
) -> ConcurrencyLevelResult:
    """Run n_requests with bounded concurrency, default n_requests = concurrency * 2."""
    if n_requests is None:
        n_requests = concurrency * 2
    result = ConcurrencyLevelResult(concurrency=concurrency, n_requests=n_requests)
    sem = asyncio.Semaphore(concurrency)
    t0 = time.perf_counter()

    async def one() -> None:
        params = ProxyParams(country=country) if country else ProxyParams()
        proxy_url = gp.build_url(params=params, mode="rotating")
        async with sem:
            req_start = time.perf_counter()
            try:
                async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client_async:
                    r = await client_async.get(target_url)
                latency = (time.perf_counter() - req_start) * 1000
                if r.status_code == 200:
                    result.successes += 1
                    result.latencies_ms.append(latency)
                elif r.status_code == 407 and "THREADS_EXHAUSTED" in r.text:
                    result.thread_exhausted += 1
                else:
                    result.other_http_errors += 1
            except httpx.TimeoutException:
                result.timeouts += 1
            except Exception:
                result.other_exceptions += 1

    await asyncio.gather(*[one() for _ in range(n_requests)])
    result.elapsed_seconds = time.perf_counter() - t0
    return result


DEFAULT_LADDER = [10, 50, 200, 500, 1000, 2000, 2500]


async def run_concurrency_ladder(
    gp: GeekproxyClient,
    *,
    ladder: list[int] | None = None,
    country: Optional[str] = None,
) -> list[ConcurrencyLevelResult]:
    ladder = ladder or DEFAULT_LADDER
    results: list[ConcurrencyLevelResult] = []
    for level in ladder:
        r = await measure_concurrency_level(gp, level, country=country)
        results.append(r)
        # back off briefly so successive levels don't run into each other's
        # tail connections
        await asyncio.sleep(5)
    return results
