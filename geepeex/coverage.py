"""Deciding which parts of a route were ridden before, and which way round.

Everything is done in a local equirectangular projection in metres. Over the
few tens of kilometres a route covers, the error is far below the GPS tolerance
we are working to, and it makes distance and angle arithmetic ordinary.
"""

import math

EARTH_R = 6371008.8
DEG = math.pi * EARTH_R / 180.0

UNRIDDEN = 0
OPPOSITE = 1
SAME = 2


class Projection:
    def __init__(self, ref_lat):
        self.kx = DEG * math.cos(math.radians(ref_lat))
        self.ky = DEG

    def __call__(self, lat, lon):
        return (lon * self.kx, lat * self.ky)


def densify(points, projection, max_len):
    """Insert points so no gap exceeds max_len.

    Colour runs are per segment, so without this a route recorded one point per
    200m would be coloured in 200m blocks regardless of tolerance.
    """
    if not points:
        return []
    out = [points[0]]
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        x1, y1 = projection(lat1, lon1)
        x2, y2 = projection(lat2, lon2)
        length = math.hypot(x2 - x1, y2 - y1)
        for step in range(1, int(length // max_len) + 1):
            f = step * max_len / length
            out.append((lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f))
        out.append((lat2, lon2))
    return out


def _segments(points, projection, max_len, max_gap):
    """Projected sub-segments, dropping recording gaps.

    A paused-then-resumed track has a straight jump across it that was never
    ridden; densifying that jump would paint coverage along a line the rider
    never took.
    """
    segs = []
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        x1, y1 = projection(lat1, lon1)
        x2, y2 = projection(lat2, lon2)
        length = math.hypot(x2 - x1, y2 - y1)
        if length == 0 or (max_gap and length > max_gap):
            continue
        pieces = max(1, int(math.ceil(length / max_len)))
        for i in range(pieces):
            f1, f2 = i / pieces, (i + 1) / pieces
            segs.append(
                (
                    x1 + (x2 - x1) * f1,
                    y1 + (y2 - y1) * f1,
                    x1 + (x2 - x1) * f2,
                    y1 + (y2 - y1) * f2,
                )
            )
    return segs


class _Grid:
    """Uniform bucket index on segment midpoints. A route plus a season of
    training is millions of segment pairs; the naive comparison is not viable."""

    def __init__(self, segments, cell):
        self.cell = cell
        self.segments = segments
        self.buckets = {}
        for i, (ax, ay, bx, by) in enumerate(segments):
            key = (int((ax + bx) / 2 // cell), int((ay + by) / 2 // cell))
            self.buckets.setdefault(key, []).append(i)

    def near(self, x, y):
        cx, cy = int(x // self.cell), int(y // self.cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.buckets.get((cx + dx, cy + dy), ()):
                    yield self.segments[i]


def _point_segment_distance(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    denom = vx * vx + vy * vy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / denom))
    return math.hypot(px - (ax + vx * t), py - (ay + vy * t))


def classify(main, training, tolerance=25.0, angle=45.0, resolution=15.0, max_gap=200.0):
    """Label each sub-segment of `main` UNRIDDEN, OPPOSITE or SAME.

    Returns (points, labels) where points is the densified main route and
    labels[i] describes the segment from points[i] to points[i+1].

    A training pass only counts if it is roughly parallel to the route. Without
    the angle test, every road crossing the route would mark it as ridden.
    """
    if len(main) < 2:
        return list(main), []

    projection = Projection(sum(lat for lat, _ in main) / len(main))
    points = densify(main, projection, resolution)

    training_segments = []
    for track in training:
        training_segments.extend(_segments(track, projection, resolution, max_gap))

    # Buckets are keyed on midpoints, so the cell has to be wide enough that a
    # match within `tolerance` of the segment body cannot sit outside the 3x3
    # neighbourhood we scan.
    grid = _Grid(training_segments, max(tolerance + resolution, 1.0))
    cos_limit = math.cos(math.radians(angle))

    labels = []
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        x1, y1 = projection(lat1, lon1)
        x2, y2 = projection(lat2, lon2)
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        ux, uy = x2 - x1, y2 - y1
        norm = math.hypot(ux, uy)

        label = UNRIDDEN
        for ax, ay, bx, by in grid.near(mx, my):
            if _point_segment_distance(mx, my, ax, ay, bx, by) > tolerance:
                continue
            vx, vy = bx - ax, by - ay
            vnorm = math.hypot(vx, vy)
            if norm == 0 or vnorm == 0:
                continue
            cos = (ux * vx + uy * vy) / (norm * vnorm)
            if cos >= cos_limit:
                label = SAME
                break  # nothing outranks a pass in the route's own direction
            if cos <= -cos_limit:
                label = OPPOSITE
        labels.append(label)

    return points, labels


def segment_lengths(points):
    projection = Projection(sum(lat for lat, _ in points) / len(points)) if points else None
    out = []
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        x1, y1 = projection(lat1, lon1)
        x2, y2 = projection(lat2, lon2)
        out.append(math.hypot(x2 - x1, y2 - y1))
    return out


def runs(points, labels):
    """Collapse consecutive same-label segments into polylines.

    One Leaflet layer per segment would be tens of thousands of objects and an
    unusable page; a route typically collapses to a few hundred runs.
    """
    out = []
    start = 0
    for i in range(1, len(labels) + 1):
        if i == len(labels) or labels[i] != labels[start]:
            out.append((labels[start], points[start : i + 1]))
            start = i
    return out
