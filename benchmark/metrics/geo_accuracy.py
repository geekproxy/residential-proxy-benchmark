"""
Geo accuracy metric.

For each proxy IP we get a `claimed country` from the proxy parameters
(or from the targeting we asked for) and three independent geolocation
opinions: ipinfo.io, ip-api.com, ipapi.co. We then compute agreement.

All three sources have generous free tiers. ipinfo.io may rate-limit
above ~50k/month, ip-api.com at 45 req/min for free.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx


GEO_SOURCES = {
    "ipinfo":  "https://ipinfo.io/{ip}/json",
    "ip_api":  "http://ip-api.com/json/{ip}?fields=status,country,countryCode,city,regionName,isp,as,query",
    "ipapi":   "https://ipapi.co/{ip}/json/",
}


@dataclass
class GeoOpinion:
    source: str
    country_code: Optional[str]
    city: Optional[str]
    region: Optional[str]
    asn: Optional[int]
    org: Optional[str]
    error: Optional[str] = None


@dataclass
class GeoVerdict:
    ip: str
    claimed_country: Optional[str]
    opinions: list[GeoOpinion]

    @property
    def consensus_country(self) -> Optional[str]:
        """Majority vote across sources that returned a country."""
        votes: dict[str, int] = {}
        for o in self.opinions:
            if o.country_code:
                code = o.country_code.upper()
                votes[code] = votes.get(code, 0) + 1
        if not votes:
            return None
        return max(votes, key=votes.get)

    @property
    def matches_claim(self) -> Optional[bool]:
        if self.claimed_country is None or self.consensus_country is None:
            return None
        return self.claimed_country.upper() == self.consensus_country.upper()


async def lookup_geo(
    ip: str,
    claimed_country: Optional[str] = None,
    *,
    timeout: float = 10.0,
) -> GeoVerdict:
    """Query all three sources concurrently and return a verdict."""
    async with httpx.AsyncClient(timeout=timeout) as client_async:
        tasks = [
            _fetch_opinion(client_async, source, url.format(ip=ip))
            for source, url in GEO_SOURCES.items()
        ]
        opinions = await asyncio.gather(*tasks)
    return GeoVerdict(ip=ip, claimed_country=claimed_country, opinions=opinions)


async def _fetch_opinion(
    client_async: httpx.AsyncClient,
    source: str,
    url: str,
) -> GeoOpinion:
    try:
        r = await client_async.get(url)
        if r.status_code >= 400:
            return GeoOpinion(source=source, country_code=None, city=None,
                              region=None, asn=None, org=None,
                              error=f"HTTP {r.status_code}")
        data = r.json()
        return _parse(source, data)
    except Exception as e:
        return GeoOpinion(source=source, country_code=None, city=None,
                          region=None, asn=None, org=None,
                          error=f"{type(e).__name__}: {e}")


def _parse(source: str, data: dict) -> GeoOpinion:
    if source == "ipinfo":
        org = data.get("org", "")
        asn = _extract_asn(org)
        return GeoOpinion(
            source=source,
            country_code=data.get("country"),
            city=data.get("city"),
            region=data.get("region"),
            asn=asn,
            org=org,
        )
    if source == "ip_api":
        if data.get("status") != "success":
            return GeoOpinion(source=source, country_code=None, city=None,
                              region=None, asn=None, org=None,
                              error=data.get("message", "ip-api error"))
        asn = _extract_asn(data.get("as", ""))
        return GeoOpinion(
            source=source,
            country_code=data.get("countryCode"),
            city=data.get("city"),
            region=data.get("regionName"),
            asn=asn,
            org=data.get("isp") or data.get("as"),
        )
    if source == "ipapi":
        return GeoOpinion(
            source=source,
            country_code=data.get("country_code"),
            city=data.get("city"),
            region=data.get("region"),
            asn=_extract_asn(data.get("asn", "")),
            org=data.get("org"),
        )
    return GeoOpinion(source=source, country_code=None, city=None, region=None,
                      asn=None, org=None, error="unknown source")


def _extract_asn(text: str) -> Optional[int]:
    """Pull the integer ASN from strings like 'AS7922 Comcast'."""
    if not text:
        return None
    text = text.strip()
    if text.upper().startswith("AS"):
        digits = ""
        for ch in text[2:]:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            return int(digits)
    return None
