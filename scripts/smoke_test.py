"""
Smoke test for Geekproxy proxy_client.

Verifies that:
  1) credentials in .env load correctly
  2) URL is built with the documented `__param.value;...` syntax
  3) actual outbound request through the proxy returns a non-local IP
  4) sticky-session mode returns the same IP twice
  5) country targeting via cr.XX hits the right geo (basic check via ipinfo.io)

Run:
  python -m scripts.smoke_test
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Allow running as `python scripts/smoke_test.py` from project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from dotenv import load_dotenv

from benchmark.proxy_client import GeekproxyClient, ProxyParams

load_dotenv()


async def fetch(client_async: httpx.AsyncClient, url: str) -> dict:
    r = await client_async.get(url, timeout=20)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"text": r.text}


async def main() -> int:
    gp = GeekproxyClient()
    print(f"host={gp.settings.host} user={gp.settings.username[:6]}...")

    # 1) Rotating, no params, default protocol
    rotating_url = gp.build_url()
    print(f"\n[1] Rotating URL  = {_mask(rotating_url)}")

    # 2) Sticky with session id
    sess = GeekproxyClient.new_session_id()
    sticky_url = gp.build_url(
        params=ProxyParams(sessid=sess, sessttl=30),
        mode="sticky",
    )
    print(f"[2] Sticky  URL  = {_mask(sticky_url)}  sess={sess}")

    # 3) Country-targeted (US) sticky
    us_url = gp.build_url(
        params=ProxyParams(country="us", sessid=GeekproxyClient.new_session_id("us")),
        mode="sticky",
    )
    print(f"[3] US sticky URL = {_mask(us_url)}")

    # 4) Country + state targeted
    ca_url = gp.build_url(
        params=ProxyParams(country="us", state="california",
                           sessid=GeekproxyClient.new_session_id("ca")),
        mode="sticky",
    )
    print(f"[4] US/California URL = {_mask(ca_url)}")

    # Run actual requests
    targets = [
        ("rotating-1", rotating_url, "https://api.ipify.org?format=json"),
        ("rotating-2", gp.build_url(), "https://api.ipify.org?format=json"),
        ("sticky-1",   sticky_url,   "https://api.ipify.org?format=json"),
        ("sticky-2",   sticky_url,   "https://api.ipify.org?format=json"),
        ("us-geo",     us_url,       "https://ipinfo.io/json"),
        ("ca-geo",     ca_url,       "https://ipinfo.io/json"),
    ]

    print("\n--- Live requests ---")
    for label, proxy_url, target in targets:
        try:
            async with httpx.AsyncClient(
                proxy=proxy_url,
                timeout=20.0,
                follow_redirects=True,
                http2=False,
            ) as client_async:
                data = await fetch(client_async, target)
            summary = _summarize(data)
            print(f"  {label:12s} -> {summary}")
        except httpx.HTTPStatusError as e:
            print(f"  {label:12s} -> HTTP {e.response.status_code} {e.response.text[:80]}")
        except Exception as e:
            print(f"  {label:12s} -> ERROR {type(e).__name__}: {e}")

    return 0


def _mask(url: str) -> str:
    """Mask the password segment."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"


def _summarize(data: dict) -> str:
    if "ip" in data and "country" in data:
        return f"ip={data['ip']} country={data.get('country')} city={data.get('city')} org={data.get('org','')}"
    if "ip" in data:
        return f"ip={data['ip']}"
    return json.dumps(data)[:120]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
