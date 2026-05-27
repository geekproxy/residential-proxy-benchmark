"""
Generic targeting-coverage metric.

The Geekproxy username grammar lets us target by country, US state,
city, or ASN. This module probes each item N times, records the actual
IP returned, cross-checks geolocation, and reports per-item:

  - hit rate (proxy returned a usable IP for this targeting)
  - geo accuracy (consensus country/region matches what we asked for)
  - pool size (unique IPs seen)
  - average latency

Used for:
  - country coverage (65+ countries -> world heat map)
  - state coverage (50 US states)
  - city coverage (~15 major cities)
  - ASN coverage (Comcast, AT&T, Verizon, BT, ...)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import httpx

from benchmark.metrics.geo_accuracy import lookup_geo
from benchmark.proxy_client import GeekproxyClient, ProxyParams


@dataclass
class CoverageProbe:
    item: str           # the country code, state name, etc.
    attempt: int
    latency_ms: Optional[float]
    ip: Optional[str]
    consensus_country: Optional[str] = None
    consensus_region: Optional[str] = None
    consensus_city: Optional[str] = None
    consensus_asn: Optional[int] = None
    consensus_org: Optional[str] = None
    matches: Optional[bool] = None
    error: Optional[str] = None


@dataclass
class CoverageReport:
    kind: str           # "country" | "state" | "city" | "asn"
    item: str
    attempts: int
    probes: list[CoverageProbe] = field(default_factory=list)

    @property
    def successful(self) -> int:
        return sum(1 for p in self.probes if p.ip)

    @property
    def matched(self) -> int:
        return sum(1 for p in self.probes if p.matches)

    @property
    def unique_ips(self) -> int:
        return len({p.ip for p in self.probes if p.ip})

    @property
    def avg_latency_ms(self) -> Optional[float]:
        vals = [p.latency_ms for p in self.probes if p.latency_ms is not None]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 1)

    def summary(self) -> dict:
        return {
            "kind": self.kind,
            "item": self.item,
            "attempts": self.attempts,
            "successful": self.successful,
            "matched": self.matched,
            "match_rate": round(self.matched / max(self.successful, 1), 3),
            "hit_rate": round(self.successful / max(self.attempts, 1), 3),
            "unique_ips": self.unique_ips,
            "avg_latency_ms": self.avg_latency_ms,
        }


ParamBuilder = Callable[[str, int], ProxyParams]
ResultMatcher = Callable[[str, CoverageProbe], Optional[bool]]


async def measure_coverage(
    gp: GeekproxyClient,
    *,
    kind: str,
    items: list[str],
    probes_per_item: int,
    build_params: ParamBuilder,
    matcher: ResultMatcher,
    concurrency: int = 15,
    timeout: float = 25.0,
    mode: str = "rotating",
) -> list[CoverageReport]:
    """
    Run coverage probes for each item with given concurrency.
    `build_params(item, attempt)` returns the ProxyParams to use.
    `matcher(item, probe)` returns True/False for the geo match.
    `mode` is "rotating" (port 823, fresh IP per request) or "sticky".
    """
    reports = {item: CoverageReport(kind=kind, item=item, attempts=probes_per_item)
               for item in items}
    sem = asyncio.Semaphore(concurrency)

    async def one(item: str, attempt: int) -> None:
        params = build_params(item, attempt)
        if mode == "rotating":
            # Strip sessid for rotating mode; it is only meaningful for sticky.
            params.sessid = None
            proxy_url = gp.build_url(params=params, mode="rotating")
        else:
            proxy_url = gp.build_url(
                params=params, mode="sticky",
                sticky_port_offset=attempt % 5000,
            )
        probe = CoverageProbe(item=item, attempt=attempt, latency_ms=None, ip=None)
        async with sem:
            try:
                t0 = time.perf_counter()
                async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client_async:
                    r = await client_async.get("https://api.ipify.org?format=json")
                probe.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
                if r.status_code == 200:
                    ip = r.json().get("ip")
                    probe.ip = ip
                    verdict = await lookup_geo(ip)
                    probe.consensus_country = verdict.consensus_country
                    if verdict.opinions:
                        # Take the richest opinion for region/city/asn fields.
                        for o in verdict.opinions:
                            if o.region:
                                probe.consensus_region = o.region
                            if o.city:
                                probe.consensus_city = o.city
                            if o.asn:
                                probe.consensus_asn = o.asn
                            if o.org:
                                probe.consensus_org = o.org
                    probe.matches = matcher(item, probe)
                else:
                    probe.error = f"HTTP {r.status_code}"
            except Exception as e:
                probe.error = f"{type(e).__name__}"
        reports[item].probes.append(probe)

    coros = [
        one(item, i)
        for item in items
        for i in range(probes_per_item)
    ]
    await asyncio.gather(*coros)
    return [reports[i] for i in items]


# ---------------------------------------------------------------------------
# Pre-built builders/matchers for the four targeting kinds
# ---------------------------------------------------------------------------

def country_builder(item: str, attempt: int) -> ProxyParams:
    return ProxyParams(
        country=item,
        sessid=GeekproxyClient.new_session_id(f"{item}{attempt}"),
    )


def country_matcher(item: str, probe: CoverageProbe) -> Optional[bool]:
    if probe.consensus_country is None:
        return None
    return probe.consensus_country.upper() == item.upper()


def state_builder(item: str, attempt: int) -> ProxyParams:
    return ProxyParams(
        country="us",
        state=item,
        sessid=GeekproxyClient.new_session_id(f"st{attempt}"),
    )


def state_matcher(item: str, probe: CoverageProbe) -> Optional[bool]:
    if not probe.consensus_region:
        return None
    return item.replace(" ", "").lower() in probe.consensus_region.replace(" ", "").lower()


def city_builder(item: str, attempt: int) -> ProxyParams:
    # `item` is "country|city", e.g. "us|newyork"
    country, city = item.split("|", 1)
    return ProxyParams(
        country=country,
        city=city,
        sessid=GeekproxyClient.new_session_id(f"ct{attempt}"),
    )


def city_matcher(item: str, probe: CoverageProbe) -> Optional[bool]:
    _, city = item.split("|", 1)
    if not probe.consensus_city:
        return None
    return city.replace(" ", "").lower() in probe.consensus_city.replace(" ", "").lower()


def asn_builder(item: str, attempt: int) -> ProxyParams:
    # `item` is "country|asn", e.g. "us|7922" (Geekproxy requires country
    # alongside ASN targeting -- 407 COUNTRY_REQUIRED otherwise).
    country, asn_str = item.split("|", 1)
    return ProxyParams(
        country=country,
        asn=int(asn_str),
        sessid=GeekproxyClient.new_session_id(f"asn{attempt}"),
    )


def asn_matcher(item: str, probe: CoverageProbe) -> Optional[bool]:
    _, asn_str = item.split("|", 1)
    if probe.consensus_asn is None:
        return None
    return probe.consensus_asn == int(asn_str)
