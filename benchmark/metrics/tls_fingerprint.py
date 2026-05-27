"""
TLS / HTTP fingerprint metric.

We hit a fingerprinting endpoint through the proxy and record:
  - JA3 hash    (TLS ClientHello fingerprint)
  - JA4 hash    (newer, more granular)
  - HTTP version actually negotiated (1.1 / 2 / 3)
  - HTTP/2 settings frame fingerprint (akamai_fingerprint_hash)

Endpoint used: https://tls.peet.ws/api/all — public, no key, returns
JSON with all the above plus parsed extensions.

A residential proxy that downgrades to HTTP/1.1 or carries a stale JA3
gets flagged by antibot systems immediately, even before they look at
the IP. So this metric explains *why* a proxy fails on Cloudflare even
when the IP itself is clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx


FINGERPRINT_URL = "https://tls.peet.ws/api/all"

# Known-good Chrome 131 fingerprint as of late 2025, for comparison.
# Source: tls.peet.ws own published value when accessed with stock Chrome.
CHROME_131_JA3_HASH = "cd08e31494f9531f560d64c695473da9"
CHROME_131_JA4 = "t13d1516h2_8daaf6152771_b1ff8ab2d16f"


@dataclass
class TlsFingerprint:
    ja3_hash: Optional[str] = None
    ja3: Optional[str] = None
    ja4: Optional[str] = None
    akamai_hash: Optional[str] = None
    http_version: Optional[str] = None
    user_agent: Optional[str] = None
    tls_version: Optional[str] = None
    matches_chrome_ja3: Optional[bool] = None
    matches_chrome_ja4: Optional[bool] = None
    raw: dict | None = None
    error: Optional[str] = None


async def measure_tls_fingerprint(
    proxy_url: str,
    *,
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    timeout: float = 25.0,
    http2: bool = True,
) -> TlsFingerprint:
    fp = TlsFingerprint(user_agent=user_agent)
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=timeout,
            http2=http2,
            headers={"User-Agent": user_agent},
        ) as client_async:
            r = await client_async.get(FINGERPRINT_URL)
        if r.status_code != 200:
            fp.error = f"HTTP {r.status_code}"
            return fp
        data = r.json()
        fp.raw = data
        tls = data.get("tls", {})
        http2_block = data.get("http2", {})
        fp.ja3 = tls.get("ja3")
        fp.ja3_hash = tls.get("ja3_hash")
        fp.ja4 = tls.get("ja4")
        fp.tls_version = tls.get("tls_version_negotiated")
        fp.akamai_hash = http2_block.get("akamai_fingerprint_hash")
        fp.http_version = data.get("http_version") or data.get("tls", {}).get("version")
        if fp.ja3_hash:
            fp.matches_chrome_ja3 = fp.ja3_hash == CHROME_131_JA3_HASH
        if fp.ja4:
            fp.matches_chrome_ja4 = fp.ja4 == CHROME_131_JA4
        return fp
    except Exception as e:
        fp.error = f"{type(e).__name__}: {e}"
        return fp
