"""
Geekproxy residential proxy client.

Wraps the full Geekproxy username-parameter grammar documented at
https://geekproxy.io/doc/residential-proxy-tutorial so the rest of the
benchmark code can target any combination of country, city, state, ZIP,
ASN, sticky session, TTL and the high-anonymity flag without rebuilding
the URL by hand.

Param syntax: login__param1.value;param2.value
Example:      login__cr.us;state.california;sessid.abc123;sessttl.60
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class GeekproxySettings(BaseSettings):
    """Loaded from .env via pydantic-settings."""

    host: str = "rs.geekproxy.io"
    sticky_port: int = 10000
    rotating_http_port: int = 823
    rotating_socks5_port: int = 824
    username: str
    password: str
    user_agent: str = "Geekproxy-Benchmark/1.0"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="GEEKPROXY_",
        extra="ignore",
    )


Protocol = Literal["http", "https", "socks5"]
Mode = Literal["rotating", "sticky"]


@dataclass
class ProxyParams:
    """All username parameters supported by Geekproxy residential plan."""

    # Free targeting
    country: Optional[str] = None              # cr.XX (single ISO-2 lowercase)
    countries: Optional[list[str]] = None      # cr.us,de,fr (multi)
    exclude_countries: Optional[list[str]] = None  # nocr.XX
    exclude_asns: Optional[list[int]] = None   # noasn.ASXXXXX

    # Paid targeting (double cost)
    state: Optional[str] = None                # state.california
    city: Optional[str] = None                 # city.berlin
    zip_code: Optional[str] = None             # zip.10001 (5 digits)
    asn: Optional[int] = None                  # asn.7922

    # Session control
    sessid: Optional[str] = None               # sticky session ID
    sessttl: Optional[int] = None              # rotation interval in minutes (1-120)

    # Anonymity
    anon: bool = False                         # high-anonymity only

    def to_username_suffix(self) -> str:
        """Build the `__param1.value;param2.value` suffix."""
        parts: list[str] = []

        if self.country:
            parts.append(f"cr.{self.country.lower()}")
        elif self.countries:
            joined = ",".join(c.lower() for c in self.countries)
            parts.append(f"cr.{joined}")

        if self.exclude_countries:
            for c in self.exclude_countries:
                parts.append(f"nocr.{c.lower()}")

        if self.exclude_asns:
            for a in self.exclude_asns:
                parts.append(f"noasn.AS{a}")

        if self.state:
            parts.append(f"state.{self._normalize(self.state)}")
        if self.city:
            parts.append(f"city.{self._normalize(self.city)}")
        if self.zip_code:
            if not (self.zip_code.isdigit() and len(self.zip_code) == 5):
                raise ValueError("zip_code must be exactly 5 digits")
            parts.append(f"zip.{self.zip_code}")
        if self.asn is not None:
            parts.append(f"asn.{self.asn}")

        if self.sessid:
            parts.append(f"sessid.{self.sessid}")
        if self.sessttl is not None:
            if not (1 <= self.sessttl <= 120):
                raise ValueError("sessttl must be between 1 and 120 minutes")
            parts.append(f"sessttl.{self.sessttl}")

        if self.anon:
            parts.append("anon")

        if not parts:
            return ""
        return "__" + ";".join(parts)

    @staticmethod
    def _normalize(value: str) -> str:
        """Geekproxy expects lowercase names without spaces."""
        return value.strip().lower().replace(" ", "")


@dataclass
class GeekproxyClient:
    """Builds proxy URLs for the Geekproxy residential plan."""

    settings: GeekproxySettings = field(default_factory=GeekproxySettings)

    def build_url(
        self,
        params: Optional[ProxyParams] = None,
        protocol: Protocol = "http",
        mode: Mode = "rotating",
        sticky_port_offset: int = 0,
    ) -> str:
        """
        Construct a full proxy URL.

        mode="rotating": uses port 823 (HTTP/HTTPS) or 824 (SOCKS5).
        mode="sticky":   uses port 10000 + sticky_port_offset (range 10000-20000).
                         For pool-diversity tests, vary sticky_port_offset to get
                         independent sticky pools without colliding session IDs.
        """
        params = params or ProxyParams()
        username_suffix = params.to_username_suffix()
        username = f"{self.settings.username}{username_suffix}"

        if mode == "rotating":
            if protocol == "socks5":
                port = self.settings.rotating_socks5_port
            else:
                port = self.settings.rotating_http_port
        else:  # sticky
            if not (0 <= sticky_port_offset <= 10000):
                raise ValueError("sticky_port_offset must be 0..10000")
            port = self.settings.sticky_port + sticky_port_offset

        return (
            f"{protocol}://{username}:{self.settings.password}"
            f"@{self.settings.host}:{port}"
        )

    def build_httpx_proxy(
        self,
        params: Optional[ProxyParams] = None,
        protocol: Protocol = "http",
        mode: Mode = "rotating",
        sticky_port_offset: int = 0,
    ) -> dict[str, str]:
        """Return a proxy mapping suitable for httpx.AsyncClient(proxies=...)."""
        url = self.build_url(params, protocol, mode, sticky_port_offset)
        return {"http://": url, "https://": url}

    @staticmethod
    def new_session_id(prefix: str = "bench") -> str:
        """Generate a random sticky session ID."""
        return f"{prefix}{secrets.token_hex(6)}"


def iter_country_params(countries: Iterable[str]) -> Iterable[ProxyParams]:
    """Helper for country-coverage metric."""
    for code in countries:
        yield ProxyParams(country=code)
