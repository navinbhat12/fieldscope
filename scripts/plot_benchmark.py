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
        "grid": "#e4e3df", "p50": "#2a78d6", "p95": "#eb6834",
    },
    "dark": {
        "surface": "#1a1a19", "text": "#ffffff", "muted": "#c3c2b7",
        "grid": "#333330", "p50": "#3987e5", "p95": "#d95926",
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
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT
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

    OUT.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        path = OUT / f"benchmark-{theme}.svg"
        path.write_text(render(phases, theme))
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
