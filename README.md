# Geekproxy Residential Proxy Benchmark

Reproducible benchmark suite for the Geekproxy.io residential proxy pool.
Every number in the companion article comes from running the scripts in
this repo against a normal Geekproxy commercial account.

The goal is verifiable claims. The runner writes raw JSONL into the
`data/` directory, one record per probe. Anyone can re-run the same
command and compare results against the published charts.

## What gets measured

| Metric | Command | What it answers |
|---|---|---|
| Country coverage | `coverage --kind country` | Which countries actually return a usable IP, and how often the IP's real geolocation matches the country we asked for. |
| State coverage (US) | `coverage --kind state` | 50 US states via `state.X` parameter, hit rate and match rate against IP geolocation consensus. |
| City coverage | `coverage --kind city` | Major cities via `city.X`. Are we really in Berlin / Tokyo / Sao Paulo, or just somewhere in the country? |
| ASN coverage | `coverage --kind asn` | Targeting a specific ISP (Comcast, AT&T, BT, Vodafone). Does `asn.X` land on that exact ASN? |
| Pool diversity | `pool-diversity` | For a given country, how many unique IPs / /24s / /16s do N rotating requests yield. |
| Fraud / reputation | `fraud-score` | Each sampled IP looked up on proxycheck.io (and optionally IPQS, IPHub, Scamalytics). How many get flagged as proxy/VPN. |
| Browser layer | `browser --target X` | Real Chrome (nodriver / undetected-chromedriver) through Geekproxy hitting hostile sites. Used for Instagram, TikTok, LinkedIn, Steam. |
| TLS fingerprint | `tls-fingerprint` | JA3 / JA4 / HTTP2 negotiated through the proxy. Shows that the proxy does not change the client's TLS signature — your stack does. |
| Sticky session | `sticky-lifetime`, `sticky-series` | How long a session ID holds the same IP before rotating. Polls the IP on an interval until it changes. |
| HTTP probe | `probe --targets X` | Success / latency / block-indicator detection across a configured list of real-world targets. |
| Concurrency stress | `concurrency` | Walks a ladder of concurrent requests; reports success rate and latency percentiles at each level. |

## Architecture

```
benchmark/
  proxy_client.py        # Builds Geekproxy URLs with the full
                         # `username__cr.us;state.X;sessid.Y;...` grammar.
  runner.py              # CLI orchestrator. One subcommand per metric.
  config_loader.py       # YAML -> pydantic models for targets, countries,
                         # states, cities, ASNs.
  metrics/
    http_probe.py        # Core HTTP zond: status + latency + block detection.
    geo_accuracy.py      # IP geo lookup, consensus across 3 free APIs.
    pool_diversity.py    # Unique IP / /24 / /16 counter.
    coverage.py          # Generic coverage runner (country/state/city/asn).
    sticky_lifetime.py   # Session hold timer, single + parallel series.
    concurrency.py       # Concurrency ladder.
    fraud_score.py       # proxycheck.io + optional IPQS / IPHub / Scamalytics.
    tls_fingerprint.py   # JA3 / JA4 via tls.peet.ws.
    browser_probe.py     # nodriver + a local CONNECT auth-relay (Chrome
                         # can't take user:pass@host inline).
  reporters/
    json_writer.py       # JSONL append-friendly writer.
config/
  targets.yaml           # Test sites: e-commerce, search, social, Steam.
  countries.yaml         # 69 ISO codes for the coverage sweep.
  us_states.yaml         # 50 states.
  cities.yaml            # 18 country|city pairs.
  asns.yaml              # 11 residential ISPs across US / GB / DE / FR / ES.
data/                    # JSONL output, one directory per run.
scripts/
  smoke_test.py          # Standalone sanity check.
```

## Requirements

- Python 3.10+
- Google Chrome (for the browser layer)
- A Geekproxy.io residential account (free trial works for the smoke test)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env with your Geekproxy credentials
```

## Quick start

```bash
# Sanity check that credentials work and rotating + sticky URLs build.
.venv/bin/python -m benchmark.runner smoke

# Probe a single category (no real traffic billed beyond the requests).
.venv/bin/python -m benchmark.runner probe --targets baselines --requests 10

# Coverage sweeps
.venv/bin/python -m benchmark.runner coverage --kind country --probes 2
.venv/bin/python -m benchmark.runner coverage --kind state   --probes 2
.venv/bin/python -m benchmark.runner coverage --kind city    --probes 3
.venv/bin/python -m benchmark.runner coverage --kind asn     --probes 3

# Pool diversity for a specific country
.venv/bin/python -m benchmark.runner pool-diversity --requests 40 --country us

# IP reputation on 50 sampled IPs
.venv/bin/python -m benchmark.runner fraud-score --ips 50 --country us

# Browser probe through nodriver
.venv/bin/python -m benchmark.runner browser --target instagram_profile --country us
```

Every run writes to `data/<UTC_timestamp>_<label>/` with a `run_meta.json`
and one or more `*.jsonl` files containing the raw records.

## Notes on the Geekproxy username grammar

The Geekproxy residential plan exposes a rich set of targeting knobs
through the proxy username. They are documented at
<https://geekproxy.io/doc/residential-proxy-tutorial>; the syntax is:

```
login__param1.value;param2.value;...
```

| Parameter | Effect |
|---|---|
| `cr.us` | Restrict to country US. Accepts comma-separated multi-country (`cr.us,de`). |
| `nocr.cn` | Exclude a country. |
| `state.california` | Pin US state. Requires `cr.us`. |
| `city.berlin` | Pin city. Requires `cr.XX`. |
| `zip.10001` | 5-digit ZIP. |
| `asn.7922` | Pin ASN. Requires `cr.XX` too (`407 COUNTRY_REQUIRED` otherwise). |
| `sessid.abc` | Sticky session id. |
| `sessttl.30` | Session TTL in minutes (1-120). |
| `noasn.AS7018` | Exclude an ASN. |
| `anon` | Restrict to high-anonymity peers (double cost). |

State / city / zip / asn are billed at 2x rate.

Ports:
- `823` HTTP/HTTPS rotating (IP changes per request)
- `824` SOCKS5 rotating
- `10000-20000` sticky (IP held for `sessttl` minutes, default 30)

## License

MIT.
