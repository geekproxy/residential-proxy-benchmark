"""
Browser-based probe using nodriver (undetected-chromedriver successor).

Used for the JS-heavy / hostile targets where a raw HTTP request gets
rejected on the TLS/JA3 layer before the body matters: Instagram,
TikTok, LinkedIn, Steam Market.

Geekproxy needs Proxy-Authorization. Chrome cannot accept inline auth in
--proxy-server=user:pass@host:port. We solve it the standard way: bring
up a tiny local HTTP CONNECT relay that has no auth and forwards every
request to Geekproxy with the right auth header attached. Chrome talks
to 127.0.0.1, the relay talks to rs.geekproxy.io.

This keeps nodriver code clean and works for any tool that talks HTTP.
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from typing import Optional

from benchmark.config_loader import Target
from benchmark.proxy_client import GeekproxyClient, ProxyParams


# ---------------------------------------------------------------------------
# Local auth relay
# ---------------------------------------------------------------------------

class ProxyAuthRelay:
    """
    Listens on 127.0.0.1:<port>, forwards every HTTP CONNECT to upstream
    Geekproxy with a Proxy-Authorization header derived from the username
    (which encodes the targeting parameters) and password.

    One relay = one targeting profile (country/state/sticky etc.). Spin
    up several relays on different ports for parallel browser tests.
    """

    def __init__(
        self,
        upstream_host: str,
        upstream_port: int,
        upstream_username: str,
        upstream_password: str,
        local_port: int = 0,
    ):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self._auth_header = b"Proxy-Authorization: Basic " + base64.b64encode(
            f"{upstream_username}:{upstream_password}".encode()
        ) + b"\r\n"
        self.local_port = local_port
        self._server: Optional[asyncio.AbstractServer] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self.local_port,
        )
        # If 0 was requested, capture the assigned port.
        self.local_port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # Read the request line and headers from the client.
            request = await reader.readuntil(b"\r\n\r\n")
        except Exception:
            writer.close()
            return

        # Inject Proxy-Authorization if absent, then forward as-is.
        if b"proxy-authorization:" not in request.lower():
            head, _, _ = request.rpartition(b"\r\n\r\n")
            request = head + b"\r\n" + self._auth_header + b"\r\n"

        try:
            up_reader, up_writer = await asyncio.open_connection(
                self.upstream_host, self.upstream_port,
            )
        except Exception:
            writer.close()
            return

        up_writer.write(request)
        await up_writer.drain()

        # Bidirectional pipe.
        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            pipe(up_reader, writer),
            pipe(reader, up_writer),
        )


def relay_for(gp: GeekproxyClient, params: ProxyParams, mode: str = "rotating",
              sticky_port_offset: int = 0) -> ProxyAuthRelay:
    username = gp.settings.username + params.to_username_suffix()
    if mode == "sticky":
        upstream_port = gp.settings.sticky_port + sticky_port_offset
    else:
        upstream_port = gp.settings.rotating_http_port
    return ProxyAuthRelay(
        upstream_host=gp.settings.host,
        upstream_port=upstream_port,
        upstream_username=username,
        upstream_password=gp.settings.password,
    )


# ---------------------------------------------------------------------------
# Browser probe
# ---------------------------------------------------------------------------

@dataclass
class BrowserProbeResult:
    target_id: str
    target_url: str
    proxy_label: str
    success: bool
    blocked: bool
    block_reason: Optional[str] = None
    final_url: Optional[str] = None
    title: Optional[str] = None
    page_text_sample: Optional[str] = None
    screenshot_path: Optional[str] = None
    elapsed_seconds: float = 0.0
    error: Optional[str] = None
    headers_sample: dict = field(default_factory=dict)


async def probe_browser(
    target: Target,
    gp: GeekproxyClient,
    params: ProxyParams,
    *,
    mode: str = "rotating",
    sticky_port_offset: int = 0,
    headless: bool = True,
    screenshot_dir: Optional[str] = None,
    wait_seconds: float = 25.0,
    timeout: float = 90.0,
) -> BrowserProbeResult:
    """
    Open the target URL in nodriver Chrome routed through Geekproxy,
    wait for JS to settle, and check for block indicators.

    Each call spins up its own relay + Chrome so failures are isolated.
    For batch runs the caller can reuse a relay by handling it directly.
    """
    import nodriver as uc  # lazy import; nodriver pulls chrome at startup

    label = f"browser:{mode}:{params.country or 'rand'}"
    result = BrowserProbeResult(
        target_id=target.id,
        target_url=target.url,
        proxy_label=label,
        success=False,
        blocked=False,
    )

    relay = relay_for(gp, params, mode, sticky_port_offset)
    await relay.start()
    t0 = time.perf_counter()
    browser = None

    try:
        browser = await asyncio.wait_for(
            uc.start(
                headless=headless,
                sandbox=False,
                browser_args=[
                    f"--proxy-server={relay.url}",
                    "--disable-blink-features=AutomationControlled",
                    "--lang=en-US,en",
                    "--window-size=1366,860",
                ],
            ),
            timeout=30,
        )
        page = await asyncio.wait_for(browser.get(target.url), timeout=timeout)
        # Set a real desktop viewport so screenshots aren't a blank thumbnail.
        try:
            await page.evaluate(
                "window.scrollTo(0, 0);"
            )
        except Exception:
            pass
        await page.wait(wait_seconds)
        # Nudge lazy-loaders progressively, then scroll back to the top.
        try:
            for y in (300, 800, 1400, 800, 0):
                await page.evaluate(f"window.scrollTo(0, {y});")
                await asyncio.sleep(1.2)
        except Exception:
            pass

        result.title = (await page.evaluate("document.title")) or ""
        result.final_url = (await page.evaluate("location.href")) or target.url
        body_text = (await page.evaluate(
            "document.body ? document.body.innerText.slice(0, 4000) : ''"
        )) or ""
        result.page_text_sample = body_text[:1000]

        for marker in target.block_indicators:
            if marker.lower() in body_text.lower() or marker.lower() in (result.title or "").lower():
                result.blocked = True
                result.block_reason = marker
                break

        if screenshot_dir:
            import os
            os.makedirs(screenshot_dir, exist_ok=True)
            shot_path = os.path.join(
                screenshot_dir,
                f"{target.id}_{int(t0)}.png",
            )
            await page.save_screenshot(shot_path)
            result.screenshot_path = shot_path

        body_marker_ok = (
            target.success_body_contains is None
            or target.success_body_contains.lower() in body_text.lower()
        )
        result.success = bool(body_marker_ok and not result.blocked)
        return result

    except asyncio.TimeoutError:
        result.error = "browser timeout"
        return result
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        return result
    finally:
        result.elapsed_seconds = round(time.perf_counter() - t0, 2)
        if browser:
            try:
                browser.stop()
            except Exception:
                pass
        await relay.stop()
