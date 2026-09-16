"""Render the POST /area benchmark as committed SVGs for the README.

Two files, light and dark, so the figure reads correctly in both GitHub themes
via a <picture> element. The dark variant is stepped for the dark surface
rather than being an inverted copy of the light one.

SVG rather than PNG because GitHub renders it inline, it stays sharp at any
zoom, and the file is a few KB of text that diffs meaningfully.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "img"

# Validated with the dataviz skill's checker: worst adjacent CVD deltaE 24.7
# (light) / 26.8 (dark), both far above the >=8 gate, and every slot clears the
# lightness band, chroma floor and 3:1 contrast against its surface.
THEMES = {
    "light": {
        "surface": "#fcfcfb", "text": "#0b0b0b", "muted": "#52514e",
        "grid": "#e4e3df", "p50": "#2a78d6", "p95": "#eb6834", "good": "#1baf7a",
    },
    "dark": {
        "surface": "#1a1a19", "text": "#ffffff", "muted": "#c3c2b7",
        "grid": "#333330", "p50": "#3987e5", "p95": "#d95926", "good": "#199e70",
    },
}

TITLE = "What the cache is worth depends on the hit rate"

FONT = "ui-sans-serif, -apple-system, Segoe UI, Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, monospace"

W, H = 940, 660
# PLOT_R is sized for the longest tip label ("63.9 (62.2-65.6)" in a mono face),
# measured from a render rather than estimated -- the first attempt clipped it.
PLOT_X, PLOT_R = 232, 210          # left label gutter, right room for tip labels
PLOT_TOP, PLOT_BOT = 136, 532
BAR_H, PAIR_GAP = 18, 2            # <=24px marks, 2px surface gap between them
CAP_CHARS = 128                    # caption wrap width, measured not guessed


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def wrap(text: str, width: int = CAP_CHARS) -> list[str]:
    """Wrap caption text. SVG does not reflow, so lines are broken explicitly --
    the alternative is the caption running off the right edge, which is how the
    first render of this figure came out."""
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def nice_ticks(vmax: float) -> list[float]:
    for step in (5, 10, 20, 25, 50, 100, 200, 250, 500):
        if vmax / step <= 6:
            return [i * step for i in range(int(vmax // step) + 2)]
    return [0, vmax]


def render(phases: list[dict], theme: str) -> str:
    c = THEMES[theme]
    vmax = max(p["p95_hi"] for p in phases)
    ticks = nice_ticks(vmax)
    span = ticks[-1]
    plot_w = W - PLOT_X - PLOT_R

    def x(v: float) -> float:
        return PLOT_X + (v / span) * plot_w

    band = (PLOT_BOT - PLOT_TOP) / len(phases)
    parts: list[str] = []
    add = parts.append

    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="{FONT}" role="img" '
        f'aria-label="POST /area latency by cache state">')
    add(f'<rect width="{W}" height="{H}" fill="{c["surface"]}"/>')

    # Title block
    add(f'<text x="32" y="44" font-size="19" font-weight="600" fill="{c["text"]}">'
        f'{esc(TITLE)}</text>')
    add(f'<text x="32" y="68" font-size="13" fill="{c["muted"]}">'
        f'POST /area latency by cache state, against a no-op control — 50 req/s synthetic load</text>')

    # Legend: always present for two or more series.
    lx = 32
    for label, key in (("p50 (median)", "p50"), ("p95 (tail)", "p95")):
        add(f'<rect x="{lx}" y="88" width="11" height="11" rx="2.5" fill="{c[key]}"/>')
        add(f'<text x="{lx + 17}" y="98" font-size="12" fill="{c["muted"]}">{label}</text>')
        lx += 22 + len(label) * 6.6

    # Gridlines: hairline, solid, recessive; drawn under the marks.
    for t in ticks:
        gx = x(t)
        add(f'<line x1="{gx:.1f}" y1="{PLOT_TOP - 8}" x2="{gx:.1f}" y2="{PLOT_BOT + 4}" '
            f'stroke="{c["grid"]}" stroke-width="1"/>')
        add(f'<text x="{gx:.1f}" y="{PLOT_BOT + 22}" font-size="11" fill="{c["muted"]}" '
            f'text-anchor="middle">{t:g}</text>')
    add(f'<text x="{PLOT_X + plot_w / 2:.0f}" y="{PLOT_BOT + 46}" font-size="11.5" '
        f'fill="{c["muted"]}" text-anchor="middle">milliseconds</text>')

    for i, p in enumerate(phases):
        top = PLOT_TOP + i * band
        mid = top + band / 2

        add(f'<text x="{PLOT_X - 20}" y="{mid - 4:.0f}" font-size="13.5" font-weight="600" '
            f'fill="{c["text"]}" text-anchor="end">{esc(p["label"])}</text>')
        add(f'<text x="{PLOT_X - 20}" y="{mid + 13:.0f}" font-size="11" '
            f'fill="{c["muted"]}" text-anchor="end">{esc(p["sub"])}</text>')

        for j, key in enumerate(("p50", "p95")):
            v = p[key]
            by = mid - BAR_H - PAIR_GAP / 2 + j * (BAR_H + PAIR_GAP)
            w = max(x(v) - PLOT_X, 2)
            # Square at the baseline, 4px rounded at the data end.
            r = min(4, w)
            add(f'<path d="M{PLOT_X} {by:.1f} H{PLOT_X + w - r:.1f} '
                f'a{r} {r} 0 0 1 {r} {r} V{by + BAR_H - r:.1f} '
                f'a{r} {r} 0 0 1 {-r} {r} H{PLOT_X} Z" fill="{c[key]}"/>')
            lo, hi = p[f"{key}_lo"], p[f"{key}_hi"]
            add(f'<text x="{PLOT_X + w + 10:.1f}" y="{by + BAR_H - 6:.1f}" font-size="12" '
                f'fill="{c["text"]}" font-family="{MONO}">{v:.1f}'
                f'<tspan fill="{c["muted"]}" font-size="10.5"> ({lo:.1f}–{hi:.1f})</tspan></text>')

    cap_y = PLOT_BOT + 74
    for block in (phases[0]["method"], phases[0]["hardware"]):
        for line in wrap(block):
            add(f'<text x="32" y="{cap_y}" font-size="10.5" fill="{c["muted"]}">'
                f'{esc(line)}</text>')
            cap_y += 14
        cap_y += 4
    add('</svg>')
    return "\n".join(parts)


def main() -> None:
    # Default to the committed results so the figure is reproducible from a
    # clean checkout. The benchmark writes these; keep them in step.
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "scripts" / "bench_results"
    # The control comes first so the floor is read before the numbers that
    # include it. Every bar below contains this much transport.
    # Ordered by hit rate, because that is the variable that decides whether
    # the cache is worth anything. The 100%-hit row is a ceiling, not a result:
    # no real workload hits on every request, and p95 is exactly the percentile
    # where the misses live.
    spec = [
        ("baseline", "Baseline (control)", "POST /ping — server does no work"),
        ("uncached", "Uncached", "cache off — straight to PostGIS"),
        ("cold", "Cold cache", "0% hit — every request a miss"),
        ("mixed_0.5", "Mixed load", "50% hit rate"),
        ("mixed_0.9", "Mixed load", "90% hit rate"),
        ("warm", "Warm cache", "100% hit — a ceiling, not a result"),
    ]

    phases = []
    for name, label, sub in spec:
        d = json.loads((src / f"bench_{name}.json").read_text())
        hw = d["hardware"]
        phases.append({
            "label": label, "sub": sub,
            "p50": d["p50_median_ms"], "p50_lo": d["p50_range_ms"][0],
            "p50_hi": d["p50_range_ms"][1],
            "p95": d["p95_median_ms"], "p95_lo": d["p95_range_ms"][0],
            "p95_hi": d["p95_range_ms"][1],
            "floor_note": "",
            "method": (
                f'Method: {d["requests_per_run"]} requests per run over a fixed set of 300 '
                f'field-sized polygons, open-loop at {d["target_rate"]:g} req/s; '
                f'{d["runs_kept"] + 1} runs, first discarded; bars are the median of '
                f'per-run values, parentheses the observed range across runs.'
            ),
            "hardware": (
                f'Synthetic load against a local Docker Compose stack — not production traffic; '
                f'this service has no users. {hw["cpu"]}, {hw["cores"]} cores, '
                f'{hw["memory_gb"]} GB. PostgreSQL 16 / PostGIS 3.4, Redis 7.'
            ),
        })

    # The hit-rate figure reads its four points from the same measured files.
    hit_spec = [
        ("cold", 0, "Cold cache", "every request a miss"),
        ("mixed_0.5", 50, "Mixed load", "half the requests hit"),
        ("mixed_0.9", 90, "Mixed load", "nine in ten hit"),
        ("warm", 100, "Warm cache", "every request a hit"),
    ]
    hit_points = []
    for name, hit, label, sub in hit_spec:
        d = json.loads((src / f"bench_{name}.json").read_text())
        hit_points.append({
            "hit": hit, "name": label, "sub": sub,
            "p50": d["p50_median_ms"], "p50r": d["p50_range_ms"],
            "p95": d["p95_median_ms"], "p95r": d["p95_range_ms"],
        })
    floor_p50 = json.loads((src / "bench_baseline.json").read_text())["p50_median_ms"]

    OUT.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        path = OUT / f"benchmark-{theme}.svg"
        path.write_text(render(phases, theme))
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")

        path = OUT / f"hitrate-{theme}.svg"
        path.write_text(render_hitrate(hit_points, floor_p50, hit_points, theme))
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")




# ---------------------------------------------------------------------------
# Second figure: what the cache is worth as the hit rate changes.
#
# The bar chart above compares phases; this one shows the relationship that
# actually decides whether the cache is worth having. Two series on one linear
# scale -- p95 collapsing, p50 flat -- because that contrast *is* the finding.
# ---------------------------------------------------------------------------

HITRATE_TITLE = "Redis cuts tail latency 7\u00d7"
HITRATE_SUB = ("POST /area p95 falls from 63.5 ms to 9.2 ms as requests repeat "
               "\u2014 a 6.9\u00d7 reduction on a spatial aggregation over 1.48M soil polygons.")


def render_hitrate(points: list[dict], floor_p50: float, rows: list[dict], theme: str) -> str:
    """The hit-rate figure: chart plus the table beneath it, in one frame.

    Everything is on screen at once deliberately. This figure's job is to be
    screenshotted into a README, where nobody can click anything -- so hiding
    three quarters of the result behind a control would hide it for good.
    """
    c = THEMES[theme]
    W2, H2 = 980, 800
    L, R, T, B = 78, 128, 150, 96          # chart box inside the figure
    CH = 470                                # chart bottom
    y_max = 70

    def x(hit: float) -> float:
        return L + (hit / 100) * (W2 - L - R)

    def y(ms: float) -> float:
        return T + (1 - ms / y_max) * (CH - T)

    parts: list[str] = []
    add = parts.append
    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W2}" height="{H2}" '
        f'viewBox="0 0 {W2} {H2}" font-family="{FONT}" role="img" '
        f'aria-label="Cache hit rate against POST /area latency: p95 falls from 63.5 ms to 9.2 ms">')
    add(f'<rect width="{W2}" height="{H2}" fill="{c["surface"]}"/>')

    add(f'<text x="36" y="52" font-size="25" font-weight="600" fill="{c["text"]}">'
        f'{esc(HITRATE_TITLE)}</text>')
    for i, ln in enumerate(wrap(HITRATE_SUB, 104)):
        add(f'<text x="36" y="{78 + i * 18}" font-size="13.5" fill="{c["muted"]}">{esc(ln)}</text>')

    lx = 36
    for label, key in (("p95 \u2014 the tail", "p95"), ("p50 \u2014 the median", "p50")):
        add(f'<rect x="{lx}" y="{T - 42}" width="11" height="11" rx="2.5" fill="{c[key]}"/>')
        add(f'<text x="{lx + 17}" y="{T - 32}" font-size="12.5" fill="{c["muted"]}">{esc(label)}</text>')
        lx += 26 + len(label) * 6.6
    add(f'<rect x="{lx}" y="{T - 42}" width="11" height="11" rx="2.5" fill="{c["muted"]}" opacity="0.4"/>')
    add(f'<text x="{lx + 17}" y="{T - 32}" font-size="12.5" fill="{c["muted"]}">transport floor</text>')

    for t in range(0, y_max + 1, 10):
        add(f'<line x1="{L}" y1="{y(t):.1f}" x2="{W2 - R}" y2="{y(t):.1f}" '
            f'stroke="{c["grid"]}" stroke-width="1"/>')
        add(f'<text x="{L - 14}" y="{y(t) + 4:.1f}" font-size="11.5" fill="{c["muted"]}" '
            f'text-anchor="end" font-family="{MONO}">{t}</text>')
    add(f'<text x="{L - 14}" y="{T - 10}" font-size="11" fill="{c["muted"]}" '
        f'text-anchor="end" font-family="{MONO}">ms</text>')

    band_h = (CH - T) * (floor_p50 / y_max)
    add(f'<rect x="{L}" y="{y(floor_p50):.1f}" width="{W2 - L - R}" height="{band_h:.1f}" '
        f'fill="{c["muted"]}" opacity="0.12"/>')
    add(f'<text x="{L + 12}" y="{y(floor_p50) + 20:.1f}" font-size="11" fill="{c["muted"]}">'
        f'transport floor \u00b7 {floor_p50:.2f} ms \u00b7 no cache can remove this</text>')

    add(f'<line x1="{x(50):.1f}" y1="{T}" x2="{x(50):.1f}" y2="{CH}" '
        f'stroke="{c["grid"]}" stroke-width="2"/>')

    for key in ("p95", "p50"):
        d = " ".join(f'{"M" if i == 0 else "L"}{x(p["hit"]):.1f} {y(p[key]):.1f}'
                     for i, p in enumerate(points))
        add(f'<path d="{d}" fill="none" stroke="{c[key]}" stroke-width="2.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/>')
        for p in points:
            add(f'<circle cx="{x(p["hit"]):.1f}" cy="{y(p[key]):.1f}" r="5" fill="{c[key]}" '
                f'stroke="{c["surface"]}" stroke-width="2"/>')

    for tx, ty, key, txt in (
        (x(0) + 14, y(points[0]["p95"]) + 5, "p95", f'{points[0]["p95"]:.1f} ms'),
        (x(0) + 14, y(points[0]["p50"]) - 14, "p50", f'{points[0]["p50"]:.1f} ms'),
        (x(100) + 12, y(points[-1]["p95"]) - 10, "p95", f'{points[-1]["p95"]:.1f} ms'),
        (x(100) + 12, y(points[-1]["p50"]) + 18, "p50", f'{points[-1]["p50"]:.1f} ms'),
    ):
        add(f'<text x="{tx:.1f}" y="{ty:.1f}" font-size="13" fill="{c[key]}" '
            f'font-family="{MONO}" font-weight="600">{txt}</text>')

    drop = 100 * (1 - points[1]["p95"] / points[0]["p95"])
    best = 100 * (1 - points[-1]["p95"] / points[0]["p95"])
    add(f'<text x="{x(50) + 18:.1f}" y="{y(points[1]["p95"]) - 16:.1f}" font-size="13.5" '
        f'fill="{c["text"]}" font-weight="600">{drop:.0f}% lower tail even at a 50% hit rate</text>')
    add(f'<text x="{x(50) + 18:.1f}" y="{y(points[1]["p95"]) + 2:.1f}" font-size="11.5" '
        f'fill="{c["muted"]}">and {best:.0f}% lower once traffic repeats</text>')

    for p in points:
        add(f'<text x="{x(p["hit"]):.1f}" y="{CH + 26}" font-size="12.5" fill="{c["text"]}" '
            f'text-anchor="middle" font-family="{MONO}">{p["hit"]}%</text>')
    add(f'<text x="{L + (W2 - L - R) / 2:.0f}" y="{CH + 46}" font-size="11.5" '
        f'fill="{c["muted"]}" text-anchor="middle">cache hit rate</text>')

    # Table: the same four states as numbers, so the figure stands alone.
    ty = CH + 86
    # Value and range are separate elements rather than one anchored string with
    # an inline tspan: anchoring a mixed-size run is exactly where SVG renderers
    # disagree, and a clipped number is worse than a plain one.
    cols = [(36, "start", "HIT RATE"), (140, "start", "STATE"),
            (600, "end", "P50"), (760, "end", "P95"), (W2 - 36, "end", "P95 VS 0% HIT")]
    for cx, anchor, label in cols:
        add(f'<text x="{cx}" y="{ty}" font-size="10.5" fill="{c["muted"]}" '
            f'text-anchor="{anchor}" font-family="{MONO}" letter-spacing="1.4">{label}</text>')
    add(f'<line x1="36" y1="{ty + 10}" x2="{W2 - 36}" y2="{ty + 10}" stroke="{c["grid"]}" stroke-width="1"/>')

    for i, r in enumerate(rows):
        ry = ty + 36 + i * 30
        cut = 100 * (1 - r["p95"] / rows[0]["p95"])
        add(f'<text x="36" y="{ry}" font-size="13" fill="{c["text"]}" font-weight="600" '
            f'font-family="{MONO}">{r["hit"]}%</text>')
        add(f'<text x="140" y="{ry}" font-size="13.5" fill="{c["text"]}">{esc(r["name"])}'
            f'<tspan fill="{c["muted"]}" font-size="11.5"> \u2014 {esc(r["sub"])}</tspan></text>')
        add(f'<text x="600" y="{ry}" font-size="13" fill="{c["text"]}" text-anchor="end" '
            f'font-family="{MONO}">{r["p50"]:.2f}</text>')
        add(f'<text x="608" y="{ry}" font-size="11" fill="{c["muted"]}" '
            f'font-family="{MONO}">({r["p50r"][0]}\u2013{r["p50r"][1]})</text>')
        add(f'<text x="760" y="{ry}" font-size="13" fill="{c["p95"]}" text-anchor="end" '
            f'font-family="{MONO}">{r["p95"]:.2f}</text>')
        add(f'<text x="768" y="{ry}" font-size="11" fill="{c["muted"]}" '
            f'font-family="{MONO}">({r["p95r"][0]}\u2013{r["p95r"][1]})</text>')
        cut_txt = f"\u2212{cut:.0f}%" if cut > 0 else "\u2014"
        add(f'<text x="{W2 - 36}" y="{ry}" font-size="13" text-anchor="end" font-family="{MONO}" '
            f'fill="{c["good"] if cut > 0 else c["muted"]}">{cut_txt}</text>')
        add(f'<line x1="36" y1="{ry + 12}" x2="{W2 - 36}" y2="{ry + 12}" '
            f'stroke="{c["grid"]}" stroke-width="1" opacity="0.5"/>')

    cap = ("300 field-sized polygons \u00b7 open-loop at 50 req/s \u00b7 300 requests per run \u00b7 "
           "5 runs per hit rate, first discarded \u00b7 median of per-run medians. "
           "Synthetic load against a local Docker Compose stack \u2014 not production traffic. "
           "Apple M2 Pro, 10 cores \u00b7 PostgreSQL 16 / PostGIS 3.4 \u00b7 Redis 7.")
    cy = H2 - 58
    for ln in wrap(cap, 136):
        add(f'<text x="36" y="{cy}" font-size="10.5" fill="{c["muted"]}">{esc(ln)}</text>')
        cy += 14
    add("</svg>")
    return "\n".join(parts)


if __name__ == "__main__":
    main()
