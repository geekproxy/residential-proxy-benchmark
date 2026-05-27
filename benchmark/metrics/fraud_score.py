"""
IP fraud-score / reputation metric.

Queries up to three independent reputation services for each IP:

  - proxycheck.io       (no key needed for ~1000 lookups/day)
  - IPQualityScore      (requires API key, free tier 5k/mo)
  - IPHub               (requires API key, free tier 1k/day)

proxycheck runs always. The other two run only if their API key is in
the environment. Output keeps each opinion separate so we can show
their spread on the chart (residential proxies should score low
across all of them).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx


@dataclass
class FraudOpinion:
    source: str
    score: Optional[float] = None          # 0-100, higher = more suspicious
    risk_label: Optional[str] = None       # source-specific label
    is_proxy: Optional[bool] = None
    is_vpn: Optional[bool] = None
    is_tor: Optional[bool] = None
    raw: dict | None = None
    error: Optional[str] = None


@dataclass
class FraudReport:
    ip: str
    opinions: list[FraudOpinion] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "ip": self.ip,
            "opinions": [vars(o) for o in self.opinions],
            "avg_score": self._avg_score(),
            "any_flagged_as_proxy": any(o.is_proxy for o in self.opinions if o.is_proxy is not None),
        }

    def _avg_score(self) -> Optional[float]:
        scores = [o.score for o in self.opinions if o.score is not None]
        if not scores:
            return None
        return round(sum(scores) / len(scores), 1)


async def assess_ip(ip: str, timeout: float = 12.0) -> FraudReport:
    report = FraudReport(ip=ip)
    async with httpx.AsyncClient(timeout=timeout) as client_async:
        tasks = [_proxycheck(client_async, ip)]
        if os.getenv("IPQUALITYSCORE_KEY"):
            tasks.append(_ipqs(client_async, ip))
        if os.getenv("IPHUB_KEY"):
            tasks.append(_iphub(client_async, ip))
        if os.getenv("SCAMALYTICS_KEY"):
            tasks.append(_scamalytics_api(client_async, ip))
        opinions = await asyncio.gather(*tasks, return_exceptions=False)
    report.opinions.extend(opinions)
    return report


async def _proxycheck(client_async: httpx.AsyncClient, ip: str) -> FraudOpinion:
    """No-key endpoint. Returns proxy/vpn flags plus a 0-100 risk score."""
    key = os.getenv("PROXYCHECK_KEY", "")
    qs = "vpn=1&risk=1&asn=1"
    if key:
        qs += f"&key={key}"
    url = f"https://proxycheck.io/v2/{ip}?{qs}"
    try:
        r = await client_async.get(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; GeekproxyBench/1.0)",
        })
        data = r.json()
        if data.get("status") != "ok":
            return FraudOpinion(source="proxycheck",
                                 error=str(data.get("message", "error")))
        ip_data = data.get(ip, {})
        if not isinstance(ip_data, dict):
            return FraudOpinion(source="proxycheck", error="bad shape")
        is_proxy = ip_data.get("proxy") == "yes"
        risk = ip_data.get("risk")
        return FraudOpinion(
            source="proxycheck",
            score=float(risk) if risk is not None else None,
            risk_label=ip_data.get("type"),
            is_proxy=is_proxy,
            is_vpn=(ip_data.get("type", "").lower() == "vpn"),
            is_tor=(ip_data.get("type", "").lower() == "tor"),
            raw=ip_data,
        )
    except Exception as e:
        return FraudOpinion(source="proxycheck", error=f"{type(e).__name__}: {e}")


async def _ipqs(client_async: httpx.AsyncClient, ip: str) -> FraudOpinion:
    key = os.environ["IPQUALITYSCORE_KEY"]
    url = f"https://ipqualityscore.com/api/json/ip/{key}/{ip}?strictness=1"
    try:
        r = await client_async.get(url)
        data = r.json()
        if not data.get("success"):
            return FraudOpinion(source="ipqs", error=str(data.get("message")))
        return FraudOpinion(
            source="ipqs",
            score=float(data.get("fraud_score", 0)),
            risk_label=_ipqs_label(data.get("fraud_score", 0)),
            is_proxy=bool(data.get("proxy")),
            is_vpn=bool(data.get("vpn")),
            is_tor=bool(data.get("tor")),
            raw=data,
        )
    except Exception as e:
        return FraudOpinion(source="ipqs", error=f"{type(e).__name__}: {e}")


def _ipqs_label(score: float) -> str:
    if score >= 90:
        return "high_risk"
    if score >= 75:
        return "suspicious"
    if score >= 50:
        return "moderate"
    return "clean"


async def _iphub(client_async: httpx.AsyncClient, ip: str) -> FraudOpinion:
    key = os.environ["IPHUB_KEY"]
    url = f"https://v2.api.iphub.info/ip/{ip}"
    try:
        r = await client_async.get(url, headers={"X-Key": key})
        data = r.json()
        block = data.get("block")   # 0 = residential, 1 = non-resi, 2 = mixed
        return FraudOpinion(
            source="iphub",
            score=_iphub_score(block),
            risk_label={0: "clean", 1: "block", 2: "mixed"}.get(block),
            is_proxy=(block == 1),
            raw=data,
        )
    except Exception as e:
        return FraudOpinion(source="iphub", error=f"{type(e).__name__}: {e}")


def _iphub_score(block: Optional[int]) -> Optional[float]:
    return {0: 5.0, 1: 95.0, 2: 50.0}.get(block) if block is not None else None


async def _scamalytics_api(client_async: httpx.AsyncClient, ip: str) -> FraudOpinion:
    """Scamalytics paid API (free tier 5k/mo after signup)."""
    user = os.environ.get("SCAMALYTICS_USER", "")
    key = os.environ["SCAMALYTICS_KEY"]
    url = f"https://api{('-' + user) if user else ''}.scamalytics.com/v3/{user}/?key={key}&ip={ip}"
    try:
        r = await client_async.get(url)
        data = r.json()
        if data.get("status") != "ok":
            return FraudOpinion(source="scamalytics",
                                 error=str(data.get("error", "error")))
        score_data = data.get("scamalytics", {})
        score = score_data.get("scamalytics_score")
        risk = score_data.get("scamalytics_risk")
        proxies = data.get("external_datasources", {}).get("ipdata", {})
        return FraudOpinion(
            source="scamalytics",
            score=float(score) if score is not None else None,
            risk_label=risk,
            is_proxy=proxies.get("is_proxy"),
            is_vpn=proxies.get("is_vpn"),
            is_tor=proxies.get("is_tor"),
            raw=data,
        )
    except Exception as e:
        return FraudOpinion(source="scamalytics", error=f"{type(e).__name__}: {e}")
