"""
Benchmark CLI orchestrator.

Usage examples:
  python -m benchmark.runner smoke
  python -m benchmark.runner probe --targets baselines --requests 50
  python -m benchmark.runner probe --targets all --requests 100 --concurrency 20
  python -m benchmark.runner country-coverage --probes-per-country 5

Each run writes JSONL into data/<run_id>/<metric>.jsonl plus a
data/<run_id>/run_meta.json with parameters and timing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn

from benchmark.config_loader import Target, TargetSuite, load_countries, load_targets
from benchmark.metrics.concurrency import DEFAULT_LADDER, run_concurrency_ladder
from benchmark.metrics.coverage import (
    asn_builder, asn_matcher,
    city_builder, city_matcher,
    country_builder, country_matcher,
    measure_coverage,
    state_builder, state_matcher,
)
from benchmark.metrics.fraud_score import assess_ip
from benchmark.metrics.geo_accuracy import lookup_geo
from benchmark.metrics.http_probe import ProbeResult, probe_once
from benchmark.metrics.pool_diversity import measure_pool_diversity
from benchmark.metrics.sticky_lifetime import (
    measure_sticky_lifetime,
    measure_sticky_lifetime_series,
)
from benchmark.metrics.tls_fingerprint import measure_tls_fingerprint
from benchmark.proxy_client import GeekproxyClient, ProxyParams
from benchmark.reporters.json_writer import JsonlWriter

load_dotenv()
console = Console()


# ---------------------------------------------------------------------------
# Run directory helpers
# ---------------------------------------------------------------------------

def make_run_dir(label: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path("data") / f"{ts}_{label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def write_meta(run_dir: Path, meta: dict) -> None:
    (run_dir / "run_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Probe command
# ---------------------------------------------------------------------------

async def cmd_probe(
    suite: TargetSuite,
    gp: GeekproxyClient,
    *,
    target_filter: str,
    requests_per_target: int,
    concurrency: int,
    use_sticky: bool,
    country: Optional[str],
) -> None:
    # Accept YAML group names (plural), category names (singular), or a target id.
    yaml_groups = {
        "baselines": suite.baselines,
        "ecommerce": suite.ecommerce,
        "search": suite.search,
        "cloudflare_akamai": suite.cloudflare_akamai,
        "steam": suite.steam,
        "social_browser": suite.social_browser,
    }
    if target_filter == "all":
        targets = suite.all_targets(include_browser=False)
    elif target_filter in yaml_groups:
        targets = [t for t in yaml_groups[target_filter] if not t.requires_browser]
    else:
        targets = [t for t in suite.all_targets(include_browser=False)
                   if t.category == target_filter or t.id == target_filter]
    if not targets:
        console.print(f"[red]No targets matched filter: {target_filter}[/red]")
        return

    label = f"probe-{target_filter}-n{requests_per_target}"
    run_dir = make_run_dir(label)
    writer = JsonlWriter(run_dir / "probes.jsonl")
    write_meta(run_dir, {
        "command": "probe",
        "target_filter": target_filter,
        "requests_per_target": requests_per_target,
        "concurrency": concurrency,
        "use_sticky": use_sticky,
        "country": country,
        "targets": [t.id for t in targets],
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    total = len(targets) * requests_per_target
    sem = asyncio.Semaphore(concurrency)

    async def run_one(target: Target, attempt: int) -> ProbeResult:
        params = ProxyParams(country=country) if country else ProxyParams()
        if use_sticky:
            params.sessid = GeekproxyClient.new_session_id(f"{target.id[:6]}{attempt}")
            mode = "sticky"
            offset = attempt % 5000
        else:
            mode = "rotating"
            offset = 0
        proxy_url = gp.build_url(params=params, mode=mode, sticky_port_offset=offset)
        label_str = f"{mode}{':' + country if country else ''}"
        async with sem:
            result = await probe_once(
                target, proxy_url, label_str,
                attempt=attempt, user_agent=suite.defaults.user_agent,
                timeout=suite.defaults.timeout_seconds,
            )
        await writer.write(result)
        return result

    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Probing", total=total)
        coros = [
            run_one(target, i)
            for target in targets
            for i in range(requests_per_target)
        ]
        for coro in asyncio.as_completed(coros):
            await coro
            progress.update(task, advance=1)

    _summarize(run_dir, targets, requests_per_target)


def _summarize(run_dir: Path, targets: list[Target], n: int) -> None:
    """Read back the JSONL and print per-target success/latency summary."""
    by_target: dict[str, list[dict]] = {t.id: [] for t in targets}
    with open(run_dir / "probes.jsonl", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            by_target.setdefault(row["target_id"], []).append(row)

    console.print(f"\n[bold]Results written to:[/bold] {run_dir}\n")
    console.print(f"{'target':30s} {'success':>8s} {'blocked':>8s} {'error':>8s} {'p50_ms':>8s} {'p90_ms':>8s}")
    console.print("-" * 80)
    for t in targets:
        rows = by_target.get(t.id, [])
        if not rows:
            continue
        ok = sum(1 for r in rows if r["success"])
        blocked = sum(1 for r in rows if r["blocked"])
        errors = sum(1 for r in rows if r["error"])
        lats = sorted(r["latency_ms"] for r in rows if r["latency_ms"] is not None)
        p50 = _pct(lats, 0.5)
        p90 = _pct(lats, 0.9)
        console.print(
            f"{t.id:30s} {ok:>4d}/{len(rows):<3d} {blocked:>8d} {errors:>8d} "
            f"{p50:>8.0f} {p90:>8.0f}"
        )


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = int(p * (len(sorted_vals) - 1))
    return sorted_vals[idx]


# ---------------------------------------------------------------------------
# Country coverage command
# ---------------------------------------------------------------------------

async def cmd_country_coverage(
    gp: GeekproxyClient,
    *,
    probes_per_country: int,
    concurrency: int,
) -> None:
    countries = load_countries().countries
    label = f"countries-{len(countries)}-n{probes_per_country}"
    run_dir = make_run_dir(label)
    writer = JsonlWriter(run_dir / "coverage.jsonl")
    write_meta(run_dir, {
        "command": "country-coverage",
        "countries": countries,
        "probes_per_country": probes_per_country,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    sem = asyncio.Semaphore(concurrency)
    total = len(countries) * probes_per_country

    async def probe_country(code: str, attempt: int) -> None:
        proxy_url = gp.build_url(
            params=ProxyParams(
                country=code,
                sessid=GeekproxyClient.new_session_id(f"{code}{attempt}"),
            ),
            mode="sticky",
            sticky_port_offset=attempt % 5000,
        )
        async with sem:
            try:
                import httpx
                t0 = time.perf_counter()
                async with httpx.AsyncClient(proxy=proxy_url, timeout=20.0) as client_async:
                    r = await client_async.get("https://api.ipify.org?format=json")
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                if r.status_code == 200:
                    ip = r.json().get("ip")
                    verdict = await lookup_geo(ip, claimed_country=code)
                    await writer.write({
                        "claimed_country": code,
                        "attempt": attempt,
                        "latency_ms": latency_ms,
                        "ip": ip,
                        "consensus_country": verdict.consensus_country,
                        "matches": verdict.matches_claim,
                        "opinions": [vars(o) for o in verdict.opinions],
                        "success": True,
                    })
                else:
                    await writer.write({
                        "claimed_country": code,
                        "attempt": attempt,
                        "latency_ms": latency_ms,
                        "status_code": r.status_code,
                        "body": r.text[:200],
                        "success": False,
                    })
            except Exception as e:
                await writer.write({
                    "claimed_country": code,
                    "attempt": attempt,
                    "error": f"{type(e).__name__}: {e}",
                    "success": False,
                })

    with Progress(
        TextColumn("[bold green]{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Country coverage", total=total)
        coros = [
            probe_country(code, i)
            for code in countries
            for i in range(probes_per_country)
        ]
        for coro in asyncio.as_completed(coros):
            await coro
            progress.update(task, advance=1)

    console.print(f"\n[bold]Country coverage written to:[/bold] {run_dir}")


# ---------------------------------------------------------------------------
# Smoke command (re-uses scripts/smoke_test idea but consistent with runner)
# ---------------------------------------------------------------------------

async def cmd_pool_diversity(gp: GeekproxyClient, *, n: int, concurrency: int,
                              country: Optional[str]) -> None:
    run_dir = make_run_dir(f"pool-diversity-n{n}-{country or 'any'}")
    writer = JsonlWriter(run_dir / "pool.jsonl")
    console.print(f"[cyan]Rotating {n} requests, country={country or 'any'}...[/cyan]")
    result = await measure_pool_diversity(gp, n, country=country, concurrency=concurrency)
    await writer.write(result.summary())
    for ip in result.ips:
        await writer.write({"ip": ip})
    write_meta(run_dir, {
        "command": "pool-diversity",
        "n": n, "country": country, "concurrency": concurrency,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    console.print(json.dumps(result.summary(), indent=2))
    console.print(f"\n[bold]Written to:[/bold] {run_dir}")


async def cmd_sticky_lifetime(gp: GeekproxyClient, *, ttl: int, poll: int,
                                country: Optional[str]) -> None:
    run_dir = make_run_dir(f"sticky-lifetime-ttl{ttl}")
    writer = JsonlWriter(run_dir / "sticky.jsonl")
    console.print(f"[cyan]Holding sticky session, requested TTL={ttl} min, poll every {poll} s...[/cyan]")
    result = await measure_sticky_lifetime(
        gp, requested_ttl_minutes=ttl, poll_interval_seconds=poll, country=country,
    )
    await writer.write(result.summary())
    for p in result.probes:
        await writer.write(vars(p))
    write_meta(run_dir, {
        "command": "sticky-lifetime",
        "ttl": ttl, "poll": poll, "country": country,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    console.print(json.dumps(result.summary(), indent=2))


async def cmd_concurrency(gp: GeekproxyClient, *, country: Optional[str],
                           ladder: Optional[list[int]]) -> None:
    run_dir = make_run_dir(f"concurrency-{country or 'any'}")
    writer = JsonlWriter(run_dir / "concurrency.jsonl")
    console.print(f"[cyan]Concurrency ladder: {ladder or DEFAULT_LADDER}, country={country or 'any'}...[/cyan]")
    results = await run_concurrency_ladder(gp, ladder=ladder, country=country)
    for r in results:
        await writer.write(r.summary())
        console.print(json.dumps(r.summary(), indent=2))
    write_meta(run_dir, {
        "command": "concurrency",
        "ladder": ladder or DEFAULT_LADDER, "country": country,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })


async def cmd_fraud_score(gp: GeekproxyClient, *, n_ips: int,
                           country: Optional[str]) -> None:
    """Sample n IPs from the pool and assess each."""
    import httpx
    run_dir = make_run_dir(f"fraud-n{n_ips}-{country or 'any'}")
    writer = JsonlWriter(run_dir / "fraud.jsonl")
    console.print(f"[cyan]Sampling {n_ips} IPs and assessing reputation...[/cyan]")
    ips: list[str] = []
    for _ in range(n_ips):
        params = ProxyParams(country=country) if country else ProxyParams()
        proxy_url = gp.build_url(params=params, mode="rotating")
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=20.0) as client_async:
                r = await client_async.get("https://api.ipify.org?format=json")
            ip = r.json().get("ip") if r.status_code == 200 else None
        except Exception:
            ip = None
        if ip and ip not in ips:
            ips.append(ip)
    for ip in ips:
        report = await assess_ip(ip)
        await writer.write(report.summary())
        console.print(f"  {ip}: avg_score={report._avg_score()} flagged={report.summary()['any_flagged_as_proxy']}")
    write_meta(run_dir, {
        "command": "fraud-score",
        "n_ips": n_ips, "actual_unique": len(ips), "country": country,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })


async def cmd_tls_fingerprint(gp: GeekproxyClient, *, country: Optional[str]) -> None:
    run_dir = make_run_dir(f"tls-fp-{country or 'any'}")
    writer = JsonlWriter(run_dir / "tls.jsonl")
    proxy_url = gp.build_url(
        params=ProxyParams(country=country) if country else ProxyParams(),
        mode="rotating",
    )
    console.print("[cyan]Measuring TLS / HTTP fingerprint via tls.peet.ws...[/cyan]")
    fp = await measure_tls_fingerprint(proxy_url)
    payload = {k: v for k, v in vars(fp).items() if k != "raw"}
    payload["raw"] = fp.raw
    await writer.write(payload)
    console.print(json.dumps({
        "ja3_hash": fp.ja3_hash,
        "ja4": fp.ja4,
        "akamai_hash": fp.akamai_hash,
        "http_version": fp.http_version,
        "tls_version": fp.tls_version,
        "matches_chrome_ja3": fp.matches_chrome_ja3,
        "matches_chrome_ja4": fp.matches_chrome_ja4,
        "error": fp.error,
    }, indent=2))


async def cmd_browser(suite: TargetSuite, gp: GeekproxyClient, *,
                       target_id: str, country: Optional[str],
                       screenshot: bool) -> None:
    from benchmark.metrics.browser_probe import probe_browser
    target = suite.by_id(target_id)
    if not target:
        console.print(f"[red]No target with id={target_id}[/red]")
        return
    run_dir = make_run_dir(f"browser-{target_id}")
    writer = JsonlWriter(run_dir / "browser.jsonl")
    shots = str(run_dir / "screenshots") if screenshot else None
    console.print(f"[cyan]Browser probe: {target.name} country={country or 'rand'}[/cyan]")
    result = await probe_browser(
        target, gp,
        ProxyParams(country=country) if country else ProxyParams(),
        screenshot_dir=shots,
    )
    await writer.write(vars(result))
    console.print(json.dumps({
        "success": result.success,
        "blocked": result.blocked,
        "block_reason": result.block_reason,
        "title": result.title,
        "final_url": result.final_url,
        "screenshot": result.screenshot_path,
        "elapsed_seconds": result.elapsed_seconds,
        "error": result.error,
    }, indent=2))


async def cmd_coverage(gp: GeekproxyClient, *, kind: str, probes: int,
                        concurrency: int) -> None:
    import yaml as _yaml
    if kind == "country":
        items = load_countries().countries
        build, match = country_builder, country_matcher
    elif kind == "state":
        with open("config/us_states.yaml") as f:
            items = _yaml.safe_load(f)["states"]
        build, match = state_builder, state_matcher
    elif kind == "city":
        with open("config/cities.yaml") as f:
            items = _yaml.safe_load(f)["cities"]
        build, match = city_builder, city_matcher
    elif kind == "asn":
        with open("config/asns.yaml") as f:
            data = _yaml.safe_load(f)["asns"]
        items = [f"{a['country']}|{a['asn']}" for a in data]
        build, match = asn_builder, asn_matcher
    else:
        console.print(f"[red]Unknown coverage kind: {kind}[/red]")
        return

    run_dir = make_run_dir(f"coverage-{kind}-{len(items)}items-n{probes}")
    writer = JsonlWriter(run_dir / f"{kind}_coverage.jsonl")
    console.print(f"[cyan]Coverage {kind}: {len(items)} items, {probes} probes each[/cyan]")
    reports = await measure_coverage(
        gp, kind=kind, items=items, probes_per_item=probes,
        build_params=build, matcher=match, concurrency=concurrency,
    )
    rows = []
    for rep in reports:
        rows.append(rep.summary())
        await writer.write(rep.summary())
        for p in rep.probes:
            await writer.write({"_probe": True, **vars(p)})
    write_meta(run_dir, {
        "command": "coverage",
        "kind": kind, "items": items, "probes_per_item": probes,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    hit_rate_avg = sum(r["hit_rate"] for r in rows) / len(rows)
    match_rate_avg = sum(r["match_rate"] for r in rows) / len(rows)
    console.print(f"\n[bold]Coverage {kind} summary:[/bold]")
    console.print(f"  items tested:       {len(rows)}")
    console.print(f"  avg hit rate:       {hit_rate_avg:.1%}")
    console.print(f"  avg match rate:     {match_rate_avg:.1%}")
    console.print(f"  total IPs sampled:  {sum(r['unique_ips'] for r in rows)}")
    console.print(f"\nWritten to: {run_dir}")


async def cmd_sticky_series(gp: GeekproxyClient, *, ttl: int, poll: int,
                             n_parallel: int, country: Optional[str]) -> None:
    run_dir = make_run_dir(f"sticky-series-ttl{ttl}-n{n_parallel}")
    writer = JsonlWriter(run_dir / "sticky_series.jsonl")
    console.print(f"[cyan]Sticky series: TTL={ttl} min x {n_parallel} parallel sessions...[/cyan]")
    results = await measure_sticky_lifetime_series(
        gp, requested_ttl_minutes=ttl, poll_interval_seconds=poll,
        n_parallel=n_parallel, country=country,
    )
    summaries = [r.summary() for r in results]
    durations = [s["first_change_seconds"] for s in summaries
                 if s["first_change_seconds"] is not None]
    median = sorted(durations)[len(durations) // 2] if durations else None
    best = max(durations) if durations else None
    payload = {
        "ttl_minutes": ttl,
        "n_parallel": n_parallel,
        "country": country,
        "sessions": summaries,
        "median_actual_seconds": median,
        "best_actual_seconds": best,
        "median_actual_minutes": round(median / 60, 2) if median else None,
        "best_actual_minutes": round(best / 60, 2) if best else None,
        "retention_ratio_median": round(median / (ttl * 60), 2) if median else None,
    }
    await writer.write(payload)
    console.print(json.dumps(payload, indent=2))


async def cmd_smoke(suite: TargetSuite, gp: GeekproxyClient) -> None:
    targets = suite.baselines[:2]
    for t in targets:
        proxy_url = gp.build_url()
        result = await probe_once(
            t, proxy_url, "rotating-smoke",
            user_agent=suite.defaults.user_agent, timeout=15,
        )
        console.print(
            f"[cyan]{t.id}[/cyan] status={result.status_code} success={result.success} "
            f"latency_ms={result.latency_ms} blocked={result.blocked} error={result.error}"
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="benchmark", description="Geekproxy benchmark runner")
    sub = p.add_subparsers(dest="command", required=True)

    p_smoke = sub.add_parser("smoke", help="Minimal sanity check")

    p_probe = sub.add_parser("probe", help="Run HTTP probes against targets")
    p_probe.add_argument("--targets", default="baselines",
                         help="category id, target id, or 'all'")
    p_probe.add_argument("--requests", type=int, default=50,
                         help="probes per target")
    p_probe.add_argument("--concurrency", type=int, default=10)
    p_probe.add_argument("--sticky", action="store_true",
                         help="use sticky sessions instead of rotating")
    p_probe.add_argument("--country", default=None,
                         help="restrict to country code, e.g. 'us'")

    p_cov = sub.add_parser("country-coverage",
                            help="Probe every country in countries.yaml")
    p_cov.add_argument("--probes-per-country", type=int, default=3)
    p_cov.add_argument("--concurrency", type=int, default=10)

    p_pool = sub.add_parser("pool-diversity", help="Unique-IP count over N rotating requests")
    p_pool.add_argument("--requests", type=int, default=200)
    p_pool.add_argument("--concurrency", type=int, default=25)
    p_pool.add_argument("--country", default=None)

    p_stick = sub.add_parser("sticky-lifetime", help="Measure actual sticky-session TTL")
    p_stick.add_argument("--ttl", type=int, default=15, help="requested TTL in minutes")
    p_stick.add_argument("--poll", type=int, default=30, help="poll interval in seconds")
    p_stick.add_argument("--country", default=None)

    p_conc = sub.add_parser("concurrency", help="Concurrency stress ladder")
    p_conc.add_argument("--country", default=None)
    p_conc.add_argument("--ladder", default=None,
                        help="comma-separated levels, e.g. 10,50,200,1000")

    p_fraud = sub.add_parser("fraud-score", help="Reputation check on N pool IPs")
    p_fraud.add_argument("--ips", type=int, default=10)
    p_fraud.add_argument("--country", default=None)

    p_tls = sub.add_parser("tls-fingerprint", help="JA3/JA4 + HTTP/2 fingerprint")
    p_tls.add_argument("--country", default=None)

    p_br = sub.add_parser("browser", help="nodriver probe through Geekproxy")
    p_br.add_argument("--target", required=True, help="target id, e.g. instagram_profile")
    p_br.add_argument("--country", default=None)
    p_br.add_argument("--no-screenshot", action="store_true")

    p_cov2 = sub.add_parser("coverage", help="Generic targeting coverage (country/state/city/asn)")
    p_cov2.add_argument("--kind", required=True, choices=["country", "state", "city", "asn"])
    p_cov2.add_argument("--probes", type=int, default=3)
    p_cov2.add_argument("--concurrency", type=int, default=15)

    p_ss = sub.add_parser("sticky-series", help="N parallel sticky sessions, returns median TTL")
    p_ss.add_argument("--ttl", type=int, default=15, help="requested TTL in minutes")
    p_ss.add_argument("--poll", type=int, default=60, help="poll interval seconds")
    p_ss.add_argument("--parallel", type=int, default=5, help="parallel sessions")
    p_ss.add_argument("--country", default=None)

    return p


async def _amain(args: argparse.Namespace) -> int:
    suite = load_targets()
    gp = GeekproxyClient()

    if args.command == "smoke":
        await cmd_smoke(suite, gp)
    elif args.command == "probe":
        await cmd_probe(
            suite, gp,
            target_filter=args.targets,
            requests_per_target=args.requests,
            concurrency=args.concurrency,
            use_sticky=args.sticky,
            country=args.country,
        )
    elif args.command == "country-coverage":
        await cmd_country_coverage(
            gp,
            probes_per_country=args.probes_per_country,
            concurrency=args.concurrency,
        )
    elif args.command == "pool-diversity":
        await cmd_pool_diversity(gp, n=args.requests,
                                  concurrency=args.concurrency,
                                  country=args.country)
    elif args.command == "sticky-lifetime":
        await cmd_sticky_lifetime(gp, ttl=args.ttl, poll=args.poll,
                                   country=args.country)
    elif args.command == "concurrency":
        ladder = None
        if args.ladder:
            ladder = [int(x.strip()) for x in args.ladder.split(",") if x.strip()]
        await cmd_concurrency(gp, country=args.country, ladder=ladder)
    elif args.command == "fraud-score":
        await cmd_fraud_score(gp, n_ips=args.ips, country=args.country)
    elif args.command == "tls-fingerprint":
        await cmd_tls_fingerprint(gp, country=args.country)
    elif args.command == "browser":
        await cmd_browser(suite, gp,
                          target_id=args.target,
                          country=args.country,
                          screenshot=not args.no_screenshot)
    elif args.command == "coverage":
        await cmd_coverage(gp, kind=args.kind, probes=args.probes,
                            concurrency=args.concurrency)
    elif args.command == "sticky-series":
        await cmd_sticky_series(gp, ttl=args.ttl, poll=args.poll,
                                 n_parallel=args.parallel,
                                 country=args.country)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
