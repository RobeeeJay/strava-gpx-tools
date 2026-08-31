"""Stacked line charts sharing one x axis, as a standalone HTML page.

Geometry is computed here and emitted as SVG in viewBox coordinates; the only
client-side work is the shared crosshair. That keeps the page dependency-free
and means what is drawn is exactly what was measured.
"""

import json
from xml.sax.saxutils import escape

# Slots 1 and 2 of the reference categorical palette, validated for both
# surfaces (worst adjacent CVD dE 24.7 light / 26.8 dark).
SERIES = ("#2a78d6", "#eb6834")
SERIES_DARK = ("#3987e5", "#d95926")

W, H = 1000, 230
LEFT, RIGHT, TOP, BOTTOM = 62, 16, 18, 34


def nice_ticks(lo, hi, count=5, cover=False):
    """Round tick values, so the axis reads in numbers a person would choose."""
    import math

    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = min((m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw),
               default=magnitude * 10)
    first = math.ceil(lo / step) * step
    end = math.ceil(hi / step) * step if cover else hi
    ticks = []
    while first <= end + step * 1e-9:
        ticks.append(round(first, 10))
        first += step
    return ticks


def _scale(lo, hi, a, b):
    span = (hi - lo) or 1.0
    return lambda v: a + (v - lo) * (b - a) / span


def line_chart(chart_id, title, unit, x, series, x_max, y_lo=None, y_hi=None, fill=False):
    """One chart. `series` is a list of (name, y values, slot index)."""
    values = [v for _, ys, _ in series for v in ys]
    lo = min(values) if y_lo is None else y_lo
    hi = max(values) if y_hi is None else y_hi
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = lo - pad, hi + pad

    x_ticks = nice_ticks(0, x_max, cover=True)
    sx = _scale(0, max(x_max, x_ticks[-1]), LEFT, W - RIGHT)
    sy = _scale(lo, hi, H - BOTTOM, TOP)

    parts = [f'<svg class="plot" viewBox="0 0 {W} {H}" preserveAspectRatio="none" '
             f'role="img" aria-label="{escape(title)}">']

    for value in nice_ticks(lo, hi):
        y = sy(value)
        if not TOP <= y <= H - BOTTOM:
            continue
        parts.append(f'<line class="grid" x1="{LEFT}" y1="{y:.1f}" x2="{W - RIGHT}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick ty" x="{LEFT - 8}" y="{y + 4:.1f}">{value:g}</text>')

    for value in x_ticks:
        x_pos = sx(value)
        if not LEFT - 2 <= x_pos <= W - RIGHT + 2:
            continue
        parts.append(f'<text class="tick tx" x="{x_pos:.1f}" y="{H - BOTTOM + 20}">{value:g}</text>')

    geom = {}
    for name, ys, slot in series:
        geom[f"{chart_id}.{slot}"] = [round(sy(b), 1) for b in ys]
        points = " ".join(f"{sx(a):.1f},{sy(b):.1f}" for a, b in zip(x, ys))
        if fill:
            parts.append(f'<polygon class="area s{slot}" points="{sx(x[0]):.1f},{H - BOTTOM} '
                         f'{points} {sx(x[-1]):.1f},{H - BOTTOM}"/>')
        parts.append(f'<polyline class="line s{slot}" points="{points}"/>')

    parts.append(f'<line class="crosshair" id="{chart_id}-hair" x1="0" y1="{TOP}" '
                 f'x2="0" y2="{H - BOTTOM}" style="display:none"/>')
    for name, _, slot in series:
        parts.append(f'<circle class="dot s{slot}" id="{chart_id}-dot{slot}" r="4" '
                     f'style="display:none"/>')
    parts.append("</svg>")

    legend = ""
    if len(series) > 1:
        chips = "".join(
            f'<span class="chip"><i class="s{slot}"></i>{escape(name)}</span>'
            for name, _, slot in series
        )
        legend = f'<div class="legend">{chips}</div>'

    html = (f'<figure class="chart" data-chart="{chart_id}">'
            f'<figcaption><h2>{escape(title)}</h2><span class="unit">{escape(unit)}</span>'
            f"{legend}</figcaption>{''.join(parts)}</figure>")
    return html, geom


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light;
  --surface: #fcfcfb; --panel: #ffffff; --border: #e6e5e1;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #85837c;
  --grid: #ecebe7;
  --s0: #2a78d6; --s1: #eb6834;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface: #1a1a19; --panel: #232322; --border: #383835;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #8f8e85;
    --grid: #2f2f2d;
    --s0: #3987e5; --s1: #d95926;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface: #1a1a19; --panel: #232322; --border: #383835;
  --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #8f8e85;
  --grid: #2f2f2d;
  --s0: #3987e5; --s1: #d95926;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface); color: var(--text-primary);
       font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: var(--text-secondary); margin: 0 0 28px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
         gap: 12px; margin-bottom: 32px; }
.tile { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
        padding: 14px 16px; }
.tile .label { font-size: 12px; letter-spacing: .04em; text-transform: uppercase;
               color: var(--text-muted); }
.tile .value { font-size: 26px; font-variant-numeric: tabular-nums; margin-top: 6px; }
.tile .value.hero { font-size: 32px; }
.tile .note { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
.chart { margin: 0 0 26px; background: var(--panel); border: 1px solid var(--border);
         border-radius: 10px; padding: 14px 16px 6px; }
figcaption { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
             margin-bottom: 6px; }
figcaption h2 { font-size: 15px; margin: 0; }
.unit { font-size: 12px; color: var(--text-muted); }
.legend { margin-left: auto; display: flex; gap: 14px; }
.chip { font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 6px; }
.chip i { width: 14px; height: 2px; border-radius: 1px; display: inline-block; }
.plot { display: block; width: 100%; height: 200px; overflow: visible; }
.grid { stroke: var(--grid); stroke-width: 1; vector-effect: non-scaling-stroke; }
.line { fill: none; stroke-width: 2; vector-effect: non-scaling-stroke;
        stroke-linejoin: round; stroke-linecap: round; }
.area { stroke: none; opacity: .13; }
.tick { fill: var(--text-muted); font-size: 11px; }
.ty { text-anchor: end; } .tx { text-anchor: middle; }
.crosshair { stroke: var(--text-muted); stroke-width: 1; vector-effect: non-scaling-stroke; }
.dot { stroke: var(--panel); stroke-width: 2; }
.s0, i.s0 { color: var(--s0); }
.line.s0 { stroke: var(--s0); } .area.s0, circle.s0, i.s0 { fill: var(--s0); background: var(--s0); }
.line.s1 { stroke: var(--s1); } .area.s1, circle.s1, i.s1 { fill: var(--s1); background: var(--s1); }
.xlabel { text-align: center; font-size: 12px; color: var(--text-muted); margin: -6px 0 26px; }
#tip { position: fixed; pointer-events: none; z-index: 10; display: none;
       background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
       padding: 8px 10px; font-size: 12px; box-shadow: 0 2px 10px rgba(0,0,0,.18);
       font-variant-numeric: tabular-nums; }
#tip b { display: block; margin-bottom: 4px; font-size: 12px; }
#tip .row { display: flex; gap: 10px; }
#tip .row span:last-child { margin-left: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--border); }
th:first-child, td:first-child { text-align: left; }
th { font-size: 12px; color: var(--text-muted); font-weight: 600; }
details { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
          padding: 12px 16px; margin-bottom: 20px; }
summary { cursor: pointer; font-size: 14px; }
.foot { color: var(--text-secondary); font-size: 13px; margin-top: 26px; }
.foot code { color: var(--text-primary); }
</style>
</head>
<body>
<div class="wrap">
<h1>__TITLE__</h1>
<p class="sub">__SUBTITLE__</p>
__TILES__
__CHARTS__
<p class="xlabel">Distance (km)</p>
__TABLE__
<p class="foot">__FOOT__</p>
</div>
<div id="tip"></div>
<script>
const D = __DATA__;
const tip = document.getElementById('tip');
const charts = [...document.querySelectorAll('.chart')];
const GEO = __GEO__;

function fmt(v, d) { return v === null ? '--' : v.toFixed(d); }
function hhmm(s) {
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? h + 'h ' + String(m).padStart(2, '0') + 'm' : m + 'm';
}

function move(evt) {
  const svg = evt.currentTarget.querySelector('.plot');
  const box = svg.getBoundingClientRect();
  const vx = (evt.clientX - box.left) / box.width * GEO.w;
  const f = (vx - GEO.left) / (GEO.w - GEO.left - GEO.right);
  const i = Math.max(0, Math.min(D.km.length - 1, Math.round(f * (D.km.length - 1))));
  const px = GEO.left + (GEO.w - GEO.left - GEO.right) * i / (D.km.length - 1);

  for (const chart of charts) {
    const id = chart.dataset.chart;
    const hair = document.getElementById(id + '-hair');
    hair.setAttribute('x1', px); hair.setAttribute('x2', px);
    hair.style.display = '';
    for (const slot of [0, 1]) {
      const dot = document.getElementById(id + '-dot' + slot);
      if (!dot) continue;
      const pts = D.geom[id + '.' + slot];
      if (!pts) { continue; }
      dot.setAttribute('cx', px);
      dot.setAttribute('cy', pts[i]);
      dot.style.display = '';
    }
  }

  tip.innerHTML = '<b>' + fmt(D.km[i], 2) + ' km &middot; ' + hhmm(D.elapsed[i]) + '</b>'
    + '<div class="row"><span>Elevation</span><span>' + fmt(D.ele[i], 0) + ' m</span></div>'
    + '<div class="row"><span>Gradient</span><span>' + fmt(D.grade[i], 1) + '%</span></div>'
    + '<div class="row"><span>Speed</span><span>' + fmt(D.speed[i], 1) + ' km/h</span></div>'
    + '<div class="row"><span>Flat-equivalent</span><span>' + fmt(D.flat[i], 1) + ' km/h</span></div>';
  tip.style.display = 'block';
  const tb = tip.getBoundingClientRect();
  tip.style.left = Math.min(evt.clientX + 14, window.innerWidth - tb.width - 8) + 'px';
  tip.style.top = Math.min(evt.clientY + 14, window.innerHeight - tb.height - 8) + 'px';
}

function leave() {
  tip.style.display = 'none';
  document.querySelectorAll('.crosshair, .dot').forEach(el => el.style.display = 'none');
}

for (const chart of charts) {
  chart.addEventListener('pointermove', move);
  chart.addEventListener('pointerleave', leave);
}
</script>
</body>
</html>
"""


def page(path, title, subtitle, tiles, charts, table, foot, data, geometry):
    html = (
        PAGE.replace("__TITLE__", escape(title))
        .replace("__SUBTITLE__", escape(subtitle))
        .replace("__TILES__", tiles)
        .replace("__CHARTS__", charts)
        .replace("__TABLE__", table)
        .replace("__FOOT__", foot)
        .replace("__DATA__", json.dumps(data))
        .replace("__GEO__", json.dumps(geometry))
    )
    with open(path, "w") as fh:
        fh.write(html)
    return path
