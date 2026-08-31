"""Elevation, speed and gradient-adjusted speed along a ride, and what they say
about fatigue.

The gradient adjustment is a physics model, not a lookup table: from the
observed speed and the local gradient it solves for the power the rider must
have been putting out, then asks what speed that same power would produce on
the flat. Absolute watts depend on the assumed mass and drag, which are
guesses; the *trend* across a ride is far less sensitive to them, which is why
the fatigue read is reported as a change and not as a number of watts.
"""

import math

from .coverage import Projection

G = 9.80665

DEFAULTS = dict(
    mass=85.0,          # rider plus bike, kg
    cda=0.32,           # drag area, m^2, hoods on a road bike
    crr=0.005,          # rolling resistance, decent tyres on tarmac
    efficiency=0.97,    # drivetrain
    rho=1.225,          # air density at sea level, 15 C
)


class NotEnoughData(Exception):
    pass


def _edges(distance, i, window, first, last):
    """Indices spanning `window` metres around sample i, clamped to
    [first, last] so no window reaches across a paused recording.

    The window simply shrinks at either end. Sliding it inwards instead was
    tried, to stop a truncated window misreading the gradient of the last
    hundred metres: it turned out to be the wrong fix for that bug — taking the
    gradient as a least-squares slope rather than by differencing a moving
    average cures it at the source, and with that in place sliding changed the
    finishing gradient by 0.01% and the fade by 0.02 km/h.
    """
    from bisect import bisect_left, bisect_right

    lo_d = distance[i] - window / 2
    hi_d = distance[i] + window / 2
    lo = max(first, bisect_left(distance, lo_d, first, last + 1) - 1)
    hi = min(last, bisect_right(distance, hi_d, first, last + 1))
    return lo, hi


def _window_mean(distance, values, window, spans):
    """Moving average over a distance window, not a sample count: GPS sampling
    is uniform in time, so a fixed sample window smooths hard climbing far more
    than fast descending."""
    totals = [0.0]
    for v in values:
        totals.append(totals[-1] + v)
    out = [0.0] * len(values)
    for first, last in spans:
        for i in range(first, last + 1):
            lo, hi = _edges(distance, i, window, first, last)
            out[i] = (totals[hi + 1] - totals[lo]) / (hi + 1 - lo)
    return out


def flat_equivalent_speed(power, mass, cda, crr, efficiency, rho):
    """Speed this power would produce on level ground.

    Resistance rises monotonically with speed, so bisection is enough and can
    not miss the root the way a Newton step off a bad guess can.
    """
    if power <= 0:
        return 0.0
    target = power * efficiency
    lo, hi = 0.0, 40.0
    for _ in range(60):
        mid = (lo + hi) / 2
        resist = 0.5 * rho * cda * mid**3 + mass * G * crr * mid
        if resist < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def analyse(segments, smoothing=100.0, window=200.0, min_speed=1.0,
            max_grade=0.30, **physics):
    """Per-sample distance, elevation, speed, gradient and flat-equivalent speed.

    `segments` come from gpxfile.read_samples; each is treated separately so no
    quantity is computed across a paused recording.
    """
    params = dict(DEFAULTS, **{k: v for k, v in physics.items() if v is not None})

    points = [p for seg in segments for p in seg]
    if len(points) < 2:
        raise NotEnoughData("fewer than two track points")
    if any(p[3] is None for p in points):
        raise NotEnoughData("no timestamps in this file; speed cannot be derived")
    if any(p[2] is None for p in points):
        raise NotEnoughData("no elevation in this file; gradient cannot be derived")

    projection = Projection(sum(p[0] for p in points) / len(points))
    start = points[0][3]

    distance, elapsed, elevation, spans = [], [], [], []
    total = 0.0
    for segment in segments:
        # Each segment is its own span: no window reaches across a pause, where
        # neither speed nor gradient means anything.
        spans.append((len(distance), len(distance) + len(segment) - 1))
        previous = None
        for lat, lon, ele, time in segment:
            x, y = projection(lat, lon)
            if previous is not None:
                total += math.hypot(x - previous[0], y - previous[1])
            previous = (x, y)
            distance.append(total)
            elapsed.append((time - start).total_seconds())
            elevation.append(ele)

    # Gradient off raw GPS elevation is noise; a 5 m wobble over a 10 m sample
    # is a 50% grade. Smooth first, then difference over a window.
    # Smoothed only for the elevation trace and the climb total; the gradient
    # does not come from differencing it. Differencing a moving average
    # flattens wherever the window stops sliding, which under-read a sustained
    # 4% finishing climb as 3%.
    smoothed = _window_mean(distance, elevation, smoothing, spans)

    n = len(distance)
    cx, cy, cxx, cxy = [0.0], [0.0], [0.0], [0.0]
    for d, e in zip(distance, elevation):
        cx.append(cx[-1] + d)
        cy.append(cy[-1] + e)
        cxx.append(cxx[-1] + d * d)
        cxy.append(cxy[-1] + d * e)

    # Speed and gradient are measured over the *same* span of road. Measuring
    # them over different spans biases the power estimate wherever speed is at a
    # local extreme, which put a spike on every descent.
    speed = [0.0] * n
    grade = [0.0] * n
    for first, last in spans:
        for i in range(first, last + 1):
            lo, hi = _edges(distance, i, window, first, last)
            dt = elapsed[hi] - elapsed[lo]
            run = distance[hi] - distance[lo]
            speed[i] = run / dt if dt > 0 else 0.0

            count = hi + 1 - lo
            sx = cx[hi + 1] - cx[lo]
            sy = cy[hi + 1] - cy[lo]
            denom = count * (cxx[hi + 1] - cxx[lo]) - sx * sx
            slope = (count * (cxy[hi + 1] - cxy[lo]) - sx * sy) / denom if denom else 0.0
            grade[i] = max(-max_grade, min(max_grade, slope))

    power, flat, moving = [], [], []
    for v, g in zip(speed, grade):
        theta = math.atan(g)
        resist = (
            params["mass"] * G * (params["crr"] * math.cos(theta) + math.sin(theta))
            + 0.5 * params["rho"] * params["cda"] * v * v
        )
        watts = resist * v / params["efficiency"]
        power.append(watts)
        flat.append(flat_equivalent_speed(watts, **params))
        # Coasting and braking say nothing about how hard the rider is working,
        # and a descent-heavy second half would otherwise read as fatigue.
        moving.append(v >= min_speed and watts > 0)

    return {
        "distance": distance,
        "elapsed": elapsed,
        "elevation": elevation,
        "elevation_smoothed": smoothed,
        "speed": speed,
        "grade": grade,
        "power": power,
        "flat_speed": flat,
        "propulsive": moving,
        "params": params,
    }


def _harmonic_speed(distance, speed, propulsive, lo, hi):
    """Distance over time, which is what "average speed" means. A plain mean of
    samples would be time-weighted and flatter the slow parts."""
    metres = seconds = 0.0
    for i in range(lo, hi):
        if not (propulsive[i] and propulsive[i + 1]) or speed[i] <= 0:
            continue
        step = distance[i + 1] - distance[i]
        metres += step
        seconds += step / speed[i]
    return metres / seconds if seconds > 0 else 0.0


def fatigue(result, buckets=4):
    """Flat-equivalent speed per equal-distance bucket, plus a distance-weighted
    linear trend through the propulsive samples."""
    distance = result["distance"]
    flat = result["flat_speed"]
    propulsive = result["propulsive"]
    total = distance[-1]
    if total <= 0:
        raise NotEnoughData("the ride covers no distance")

    edges = [total * i / buckets for i in range(buckets + 1)]
    index, quarters = 0, []
    for b in range(buckets):
        lo = index
        while index < len(distance) - 1 and distance[index] < edges[b + 1]:
            index += 1
        quarters.append(
            {
                "from_km": edges[b] / 1000,
                "to_km": edges[b + 1] / 1000,
                "flat_kmh": _harmonic_speed(distance, flat, propulsive, lo, index) * 3.6,
                "actual_kmh": _harmonic_speed(distance, result["speed"], propulsive, lo, index) * 3.6,
            }
        )

    # Plain least squares. Weighting the fit by distance was tried, on the
    # reasoning that time-uniform samples over-represent slow ground on a
    # distance axis: measured against simulated rides with a known fade it made
    # no difference worth the term, even with 56% of the samples falling in 17%
    # of the road. The lever arm of the fit dominates the sample density.
    sw = sx = sy = sxx = sxy = 0.0
    for i in range(len(distance) - 1):
        if not propulsive[i] or flat[i] <= 0:
            continue
        x, y = distance[i], flat[i]
        sw += 1.0
        sx += x
        sy += y
        sxx += x * x
        sxy += x * y
    denom = sw * sxx - sx * sx
    slope = (sw * sxy - sx * sy) / denom if denom else 0.0
    intercept = (sy - slope * sx) / sw if sw else 0.0

    return {
        "quarters": quarters,
        "trend": [(0.0, intercept * 3.6), (total, (intercept + slope * total) * 3.6)],
        "per_10km_kmh": slope * 10000 * 3.6,
        "start_kmh": intercept * 3.6,
        "end_kmh": (intercept + slope * total) * 3.6,
    }
