"""
Core HTTP probe: one request through one proxy URL, returns everything we
need to derive success_rate, latency, and bot-detection signals in one
pass. Designed to be called many times by the runner.

This module is intentionally side-effect-free besides the network call.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from benchmark.config_loader import Target


@dataclass
class ProbeResult:
    target_id: str
    target_url: str
    proxy_label: str           # what describes the proxy choice ("rotating", "sticky-us-ca-1" etc.)
    attempt: int
    started_at: float          # epoch seconds
    latency_ms: Optional[float]
    status_code: Optional[int]
    success: bool
    blocked: bool              # block_indicator hit
    block_reason: Optional[str] = None
    error: Optional[str] = None
    response_bytes: Optional[int] = None
    server_header: Optional[str] = None
    cf_ray: Optional[str] = None         # Cloudflare ray ID, if present
    akamai_reference: Optional[str] = None
    headers_sample: dict[str, str] = field(default_factory=dict)


async def probe_once(
    target: Target,
    proxy_url: str,
    proxy_label: str,
    *,
    attempt: int = 0,
    user_agent: str,
    timeout: float = 30.0,
    http2: bool = False,
) -> ProbeResult:
    started = time.time()
    result = ProbeResult(
        target_id=target.id,
        target_url=target.url,
        proxy_label=proxy_label,
        attempt=attempt,
        started_at=started,
        latency_ms=None,
        status_code=None,
        success=False,
        blocked=False,
    )
    headers = {"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"}

    try:
        t0 = time.perf_counter()
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=timeout,
            follow_redirects=True,
            http2=http2,
        ) as client_async:
            r = await client_async.request(target.method, target.url, headers=headers)
        result.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        result.status_code = r.status_code
        body = r.text
        result.response_bytes = len(r.content)
        result.server_header = r.headers.get("server")
        result.cf_ray = r.headers.get("cf-ray")
        result.akamai_reference = (
            r.headers.get("x-reference-id")
            or r.headers.get("akamai-grn")
        )
        result.headers_sample = {
            k.lower(): v[:200]
            for k, v in r.headers.items()
            if k.lower() in {
                "server", "cf-ray", "cf-cache-status", "cf-mitigated",
                "x-served-by", "x-cache", "set-cookie",
                "x-akamai-edge-cache",
            }
        }

        status_ok = r.status_code in target.success_status
        body_ok = (
            target.success_body_contains is None
            or target.success_body_contains in body
        )

        for marker in target.block_indicators:
            if marker.lower() in body.lower():
                result.blocked = True
                result.block_reason = marker
                break

        result.success = bool(status_ok and body_ok and not result.blocked)
        return result

    except httpx.HTTPError as e:
        result.error = f"{type(e).__name__}: {e}"
        return result
    except Exception as e:
        result.error = f"unexpected: {type(e).__name__}: {e}"
        return result


async def probe_many(
    target: Target,
    proxy_urls: list[tuple[str, str]],   # [(proxy_url, label), ...]
    *,
    user_agent: str,
    concurrency: int = 10,
    timeout: float = 30.0,
) -> list[ProbeResult]:
    """Run many probes against one target, bounded concurrency."""
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(idx: int, proxy_url: str, label: str) -> ProbeResult:
        async with sem:
            return await probe_once(
                target, proxy_url, label,
                attempt=idx, user_agent=user_agent, timeout=timeout,
            )

    tasks = [
        _bounded(i, url, label)
        for i, (url, label) in enumerate(proxy_urls)
    ]
    return await asyncio.gather(*tasks)
