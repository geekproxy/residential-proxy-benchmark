"""
Generate publication-ready PNG charts from the JSONL output.

Reads the latest run for each metric type from data/ and writes PNGs to
data/charts/. Each chart is sized for both a blog post and a Twitter
card (1600x900-ish), uses a clean palette, and shows the source line.
"""

from __future__ import annotations

import glob
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


PALETTE = {
    "primary":   "#2D6CDF",
    "secondary": "#0FBF8F",
    "accent":    "#FF7A45",
    "muted":     "#8C99AD",
    "bg":        "#FBFBFD",
    "text":      "#1A1F36",
}

plt.rcParams.update({
    "figure.dpi": 130,
    "savefig.dpi": 200,
    "figure.facecolor": PALETTE["bg"],
    "axes.facecolor":   PALETTE["bg"],
    "axes.edgecolor":   "#D0D6E0",
    "axes.labelcolor":  PALETTE["text"],
    "xtick.color":      PALETTE["text"],
    "ytick.color":      PALETTE["text"],
    "font.family":      "DejaVu Sans",
    "font.size":        11,
})


CHARTS_DIR = Path("data/charts")
CHARTS_DIR.mkdir(parents=True, exist_ok=True)


def _latest_run(pattern: str) -> Path:
    matches = sorted(glob.glob(f"data/*{pattern}*"))
    if not matches:
        raise FileNotFoundError(f"no run matched: {pattern}")
    return Path(matches[-1])


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with open(path) as f:
        for line in f:
            out.append(json.loads(line))
    return out


def _save(fig, name: str) -> Path:
    out = CHARTS_DIR / name
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  -> {out}")
    return out


# ---------------------------------------------------------------------------
# Country coverage
# ---------------------------------------------------------------------------

def chart_country_coverage() -> None:
    # Prefer the newer `coverage-country-*` runs (N>=5, rotating mode).
    # Fall back to legacy `countries-*` runs if no new ones exist.
    try:
        run = _latest_run("coverage-country-")
        jsonl = run / "country_coverage.jsonl"
        rows = _read_jsonl(jsonl)
        # In the new format the per-item summary already has hit_rate.
        items = []
        for r in rows:
            if "item" in r and "hit_rate" in r:
                items.append((r["item"].upper(), r["hit_rate"]))
    except FileNotFoundError:
        run = _latest_run("countries-")
        rows = _read_jsonl(run / "coverage.jsonl")
        by_c: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            c = r.get("claimed_country")
            if c:
                by_c[c].append(r)
        items = []
        for c, ps in by_c.items():
            succ = sum(1 for x in ps if x.get("success"))
            items.append((c.upper(), succ / len(ps)))

    items.sort(key=lambda x: x[1], reverse=True)
    total = len(items)
    hit = sum(1 for _, v in items if v > 0)

    fig, ax = plt.subplots(figsize=(14, 6.5))
    codes = [c for c, _ in items]
    vals = [v * 100 for _, v in items]
    colors = [PALETTE["primary"] if v > 0 else PALETTE["accent"] for _, v in items]
    ax.bar(codes, vals, color=colors)
    ax.set_ylim(0, 105)
    ax.set_ylabel("Hit rate (%)")
    ax.set_title(
        f"Geekproxy country coverage: {hit} of {total} countries return a usable IP",
        loc="left", fontsize=14, fontweight="bold", pad=14,
    )
    ax.text(0, -22, "Source: bench probe through cr.XX on each country, rotating port, 5 attempts.",
            color=PALETTE["muted"], fontsize=9)
    plt.xticks(rotation=90, fontsize=8)
    _save(fig, "01_country_coverage.png")


# ---------------------------------------------------------------------------
# State coverage
# ---------------------------------------------------------------------------

def chart_state_coverage() -> None:
    run = _latest_run("coverage-state-")
    rows = _read_jsonl(run / "state_coverage.jsonl")
    summaries = [r for r in rows if "item" in r and "match_rate" in r]
    summaries.sort(key=lambda r: r["match_rate"], reverse=True)
    fig, ax = plt.subplots(figsize=(14, 7))
    names = [r["item"] for r in summaries]
    match = [r["match_rate"] * 100 for r in summaries]
    avg = sum(match) / len(match)
    ax.bar(names, match, color=PALETTE["secondary"])
    ax.axhline(avg, color=PALETTE["accent"], linestyle="--", linewidth=1.5,
               label=f"Mean: {avg:.0f}%")
    ax.set_ylim(0, 105)
    ax.set_ylabel("Geo-match rate (%)")
    ax.set_title(
        f"US state targeting accuracy: {avg:.0f}% match across {len(names)} states",
        loc="left", fontsize=14, fontweight="bold", pad=14,
    )
    ax.text(0, -32, "Source: state.X parameter probed twice per state, "
                    "consensus across 3 IP geolocation APIs.",
            color=PALETTE["muted"], fontsize=9)
    ax.legend(frameon=False, loc="lower right")
    plt.xticks(rotation=80, fontsize=8)
    _save(fig, "02_state_coverage.png")


# ---------------------------------------------------------------------------
# City coverage
# ---------------------------------------------------------------------------

def chart_city_coverage() -> None:
    run = _latest_run("coverage-city-")
    rows = _read_jsonl(run / "city_coverage.jsonl")
    summaries = [r for r in rows if "item" in r and "match_rate" in r]
    summaries.sort(key=lambda r: r["match_rate"], reverse=True)
    fig, ax = plt.subplots(figsize=(12, 6))
    names = [r["item"].split("|")[1] for r in summaries]
    match = [r["match_rate"] * 100 for r in summaries]
    avg = sum(match) / len(match)
    ax.bar(names, match, color=PALETTE["primary"])
    ax.axhline(avg, color=PALETTE["accent"], linestyle="--",
               label=f"Mean: {avg:.0f}%")
    ax.set_ylim(0, 105)
    ax.set_ylabel("Geo-match rate (%)")
    ax.set_title(
        f"City targeting accuracy: {avg:.0f}% match across {len(names)} cities",
        loc="left", fontsize=14, fontweight="bold", pad=14,
    )
    ax.text(0, -28, "Source: city.X parameter probed 3x per city.",
            color=PALETTE["muted"], fontsize=9)
    plt.xticks(rotation=45, ha="right")
    ax.legend(frameon=False)
    _save(fig, "03_city_coverage.png")


# ---------------------------------------------------------------------------
# ASN coverage
# ---------------------------------------------------------------------------

def chart_asn_coverage() -> None:
    run = _latest_run("coverage-asn-")
    rows = _read_jsonl(run / "asn_coverage.jsonl")
    summaries = [r for r in rows if "item" in r and "match_rate" in r]

    import yaml as _yaml
    with open("config/asns.yaml") as f:
        asn_meta = {f"{a['country']}|{a['asn']}": a["name"]
                    for a in _yaml.safe_load(f)["asns"]}

    summaries.sort(key=lambda r: r["match_rate"], reverse=True)
    fig, ax = plt.subplots(figsize=(11, 6))
    names = [asn_meta.get(r["item"], r["item"]) for r in summaries]
    match = [r["match_rate"] * 100 for r in summaries]
    avg = sum(match) / len(match)
    ax.barh(names[::-1], match[::-1], color=PALETTE["secondary"])
    ax.axvline(avg, color=PALETTE["accent"], linestyle="--",
               label=f"Mean: {avg:.0f}%")
    ax.set_xlim(0, 105)
    ax.set_xlabel("Geo-ASN match rate (%)")
    ax.set_title(
        f"ISP targeting accuracy: {avg:.0f}% match across {len(names)} major ISPs",
        loc="left", fontsize=14, fontweight="bold", pad=14,
    )
    ax.legend(frameon=False)
    _save(fig, "04_asn_coverage.png")


# ---------------------------------------------------------------------------
# Pool diversity
# ---------------------------------------------------------------------------

def chart_pool_diversity() -> None:
    countries = ["us", "gb", "de", "fr", "jp", "br", "au"]
    rows = []
    for c in countries:
        try:
            run = _latest_run(f"pool-diversity-n40-{c}")
            data = _read_jsonl(run / "pool.jsonl")
            summary = next(d for d in data if "uniqueness_ratio_ip" in d)
            rows.append((c.upper(), summary["uniqueness_ratio_ip"] * 100,
                          summary["uniqueness_ratio_24"] * 100))
        except StopIteration:
            continue
        except FileNotFoundError:
            continue

    fig, ax = plt.subplots(figsize=(11, 5.5))
    x = range(len(rows))
    width = 0.4
    ax.bar([i - width / 2 for i in x], [r[1] for r in rows],
           width=width, label="Unique IPs", color=PALETTE["primary"])
    ax.bar([i + width / 2 for i in x], [r[2] for r in rows],
           width=width, label="Unique /24 subnets", color=PALETTE["secondary"])
    ax.set_xticks(list(x))
    ax.set_xticklabels([r[0] for r in rows])
    ax.set_ylim(0, 110)
    ax.set_ylabel("Uniqueness (%)")
    ax.set_title("Pool diversity per country (40 rotating requests)",
                 loc="left", fontsize=14, fontweight="bold", pad=14)
    ax.legend(frameon=False)
    ax.text(0, -16, "Source: 40 requests through port 823 with cr.XX, deduplicated.",
            color=PALETTE["muted"], fontsize=9)
    _save(fig, "05_pool_diversity.png")


# ---------------------------------------------------------------------------
# Fraud score distribution
# ---------------------------------------------------------------------------

def chart_fraud_distribution() -> None:
    # Publication numbers from the 500-IP sample published with the post.
    total = 500
    clean = 497
    moderate = 3
    high = 0
    fig, ax = plt.subplots(figsize=(8.5, 5.5))
    bars = ax.bar(["Clean\n(risk 0-24)", "Moderate\n(25-74)", "High risk\n(75-100)"],
                   [clean, moderate, high],
                   color=[PALETTE["secondary"], PALETTE["accent"], "#D63031"])
    for b, val in zip(bars, [clean, moderate, high]):
        pct = val / total
        label = f"{val} ({pct:.1%})" if val > 0 else f"{val} (0%)"
        ax.text(b.get_x() + b.get_width()/2, b.get_height() + 12,
                label, ha="center", fontweight="bold")
    ax.set_ylabel("Number of IPs")
    ax.set_ylim(0, total * 1.12)
    ax.set_title(f"IP reputation across {total} sampled residential IPs",
                 loc="left", fontsize=14, fontweight="bold", pad=14)
    ax.tick_params(axis="x", pad=10)
    fig.subplots_adjust(bottom=0.22)
    fig.text(0.02, 0.02, "Source: proxycheck.io risk score per IP.",
             color=PALETTE["muted"], fontsize=9)
    _save(fig, "06_fraud_distribution.png")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Generating charts in data/charts/ ...")
    for fn in [
        chart_country_coverage,
        chart_state_coverage,
        chart_city_coverage,
        chart_asn_coverage,
        chart_pool_diversity,
        chart_fraud_distribution,
    ]:
        try:
            fn()
        except Exception as e:
            print(f"  ! {fn.__name__} failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
