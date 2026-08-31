"""Rendering coverage results as a self-contained Leaflet page over OSM tiles."""

import json
from xml.sax.saxutils import escape

from .coverage import OPPOSITE, SAME, UNRIDDEN

COLOURS = {
    UNRIDDEN: "#f57c00",
    OPPOSITE: "#1976d2",
    SAME: "#2e7d32",
}
TRAINING_COLOUR = "#f3ff11"

# Custom panes rather than relying on the order layers happen to be added in:
# toggling the training layer off and on re-adds it, which would otherwise lift
# the grey lines above the colouring the page exists to show. Both sit between
# Leaflet's overlayPane (400) and shadowPane (500).
TRAINING_Z = 410
ROUTE_Z = 420
LABELS = {
    UNRIDDEN: "Never ridden",
    OPPOSITE: "Ridden, other direction",
    SAME: "Ridden, same direction",
}

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin: 0; height: 100%; font: 14px/1.45 system-ui, sans-serif; }
  #map { position: absolute; inset: 0; }
  .panel {
    position: absolute; top: 12px; right: 12px; z-index: 1000;
    background: rgba(255,255,255,.95); border-radius: 8px; padding: 12px 14px;
    box-shadow: 0 1px 6px rgba(0,0,0,.3); max-width: 300px;
  }
  .panel h1 { font-size: 15px; margin: 0 0 8px; }
  .row { display: flex; align-items: baseline; gap: 8px; margin: 5px 0; }
  .swatch { width: 14px; height: 4px; border-radius: 2px; flex: none; }
  .num { margin-left: auto; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .foot { margin-top: 10px; padding-top: 8px; border-top: 1px solid #ddd;
          font-size: 12px; color: #555; }
</style>
</head>
<body>
<div id="map"></div>
<div class="panel">
  <h1>__TITLE__</h1>
  __LEGEND__
  <div class="foot">__FOOT__</div>
</div>
<script>
const runs = __RUNS__;
const training = __TRAINING__;
const colours = __COLOURS__;

const map = L.map('map');
map.createPane('trainingPane').style.zIndex = __TRAINING_Z__;
map.createPane('routePane').style.zIndex = __ROUTE_Z__;

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
}).addTo(map);

const trainingLayer = L.layerGroup(
  training.map(t => L.polyline(t, {
    pane: 'trainingPane', color: __TRAINING_COLOUR__, weight: 2, opacity: .8
  }))
);

const routeLayer = L.layerGroup(
  runs.map(([label, pts]) => L.polyline(pts, {
    pane: 'routePane', color: colours[label], weight: 5, opacity: .95,
    lineCap: 'round', lineJoin: 'round'
  }))
).addTo(map);

if (training.length) {
  trainingLayer.addTo(map);
  L.control.layers(null, {'Training tracks': trainingLayer, 'Main route': routeLayer},
                   {collapsed: false, position: 'bottomleft'}).addTo(map);
}

const bounds = L.latLngBounds(runs.flatMap(([, pts]) => pts));
map.fitBounds(bounds, {padding: [30, 30]});
</script>
</body>
</html>
"""


def _km(metres):
    return f"{metres / 1000:.1f} km"


def render(path, runs, stats, title, training=(), note=""):
    total = sum(stats.values()) or 1
    legend = []
    for label in (SAME, OPPOSITE, UNRIDDEN):
        metres = stats.get(label, 0.0)
        legend.append(
            f'<div class="row"><span class="swatch" style="background:{COLOURS[label]}"></span>'
            f"<span>{LABELS[label]}</span>"
            f'<span class="num">{_km(metres)} &middot; {100 * metres / total:.0f}%</span></div>'
        )
    if training:
        legend.append(
            f'<div class="row"><span class="swatch" style="background:{TRAINING_COLOUR}"></span>'
            f"<span>All training tracks</span></div>"
        )

    html = (
        TEMPLATE.replace("__TITLE__", escape(title))
        .replace("__LEGEND__", "\n  ".join(legend))
        .replace("__FOOT__", escape(note))
        .replace("__RUNS__", json.dumps([[label, [[round(a, 6), round(b, 6)] for a, b in points]]
                                         for label, points in runs]))
        .replace("__TRAINING__", json.dumps([[[round(a, 6), round(b, 6)] for a, b in track]
                                             for track in training]))
        .replace("__COLOURS__", json.dumps(COLOURS))
        .replace("__TRAINING_COLOUR__", json.dumps(TRAINING_COLOUR))
        .replace("__TRAINING_Z__", str(TRAINING_Z))
        .replace("__ROUTE_Z__", str(ROUTE_Z))
    )
    with open(path, "w") as fh:
        fh.write(html)
    return path
