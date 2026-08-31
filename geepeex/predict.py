"""Predicting how long a route will take, from how recent rides actually went.

Each training ride is reduced to two numbers: the power the rider started it
with and how fast that power decayed as the ride went on. Those are averaged
across rides with recent ones counting for more, then played forward over the
target route's gradients.

The output is a prediction, not a measurement. It is moving time with no wind,
no drafting, no stops and no traffic, from a power model whose constants are
guesses. What it is good for is comparing routes and seeing where the time goes.
"""

import math

from . import rideprofile
from .rideprofile import flat_equivalent_speed, speed_for_power

DEFAULT_HALF_LIFE = 42.0  # days; a ride six weeks old counts half as much


class NotEnoughRides(Exception):
    pass


def ride_fitness(result, min_samples=60):
    """One ride reduced to a starting power and a decay rate, watts per hour.

    Fitted over the propulsive samples only: coasting and stopped time say
    nothing about what the rider had in them.
    """
    elapsed, power, propulsive = result["elapsed"], result["power"], result["propulsive"]
    samples = [(elapsed[i] / 3600.0, power[i]) for i in range(len(power)) if propulsive[i]]
    if len(samples) < min_samples:
        return None

    n = len(samples)
    sx = sum(x for x, _ in samples)
    sy = sum(y for _, y in samples)
    sxx = sum(x * x for x, _ in samples)
    sxy = sum(x * y for x, y in samples)
    denom = n * sxx - sx * sx
    slope = (n * sxy - sx * sy) / denom if denom else 0.0
    return {
        "start_watts": (sy - slope * sx) / n,
        "decay_watts_per_hour": slope,
        "mean_watts": sy / n,
        "hours": samples[-1][0] - samples[0][0],
        "km": result["distance"][-1] / 1000.0,
    }


def fitness(rides, half_life_days=DEFAULT_HALF_LIFE, reference=None):
    """Recency-weighted average of the per-ride fits.

    Weight is recency times duration. Recency because form fades; duration
    because a two-hour ride constrains the decay rate and a twenty-minute one
    barely constrains it at all — the fade is the whole quantity of interest,
    and a short ride simply has not been going long enough to show it.
    """
    if not rides:
        raise NotEnoughRides("no training rides had enough propulsive samples to fit")

    reference = reference or max(r["date"] for r in rides)
    total = 0.0
    start = decay = 0.0
    for ride in rides:
        age = (reference - ride["date"]).total_seconds() / 86400.0
        ride["recency"] = 0.5 ** (max(age, 0.0) / half_life_days)
        ride["weight"] = ride["recency"] * ride["hours"]
        total += ride["weight"]
        start += ride["weight"] * ride["start_watts"]
        decay += ride["weight"] * ride["decay_watts_per_hour"]
    if total <= 0:
        raise NotEnoughRides("every training ride weighed zero")

    return {
        "start_watts": start / total,
        "decay_watts_per_hour": decay / total,
        "rides": sorted(rides, key=lambda r: r["date"], reverse=True),
        "longest_hours": max(r["hours"] for r in rides),
        "half_life_days": half_life_days,
        "reference": reference,
    }


def predict(points, elevation, fit, params, window=200.0, smoothing=100.0,
            max_speed=18.0, min_speed=1.0, floor=0.5):
    """Walk the route, spending the modelled power against each gradient.

    Power falls with elapsed time, not with distance: an hour of climbing tires
    a rider more than an hour of descending covers ground.
    """
    from .coverage import Projection

    if len(points) < 2:
        raise NotEnoughRides("the route has fewer than two points")

    projection = Projection(sum(lat for lat, _ in points) / len(points))
    distance, total = [0.0], 0.0
    previous = projection(*points[0])
    for lat, lon in points[1:]:
        x, y = projection(lat, lon)
        total += math.hypot(x - previous[0], y - previous[1])
        previous = (x, y)
        distance.append(total)

    spans = [(0, len(distance) - 1)]
    grade = rideprofile.gradient(distance, elevation, spans, window)
    smoothed = rideprofile._window_mean(distance, elevation, smoothing, spans)

    lowest = fit["start_watts"] * floor
    elapsed, speeds, watts, flat = [0.0], [], [], []
    seconds = 0.0
    crawled = False
    for i in range(len(distance) - 1):
        power = fit["start_watts"] + fit["decay_watts_per_hour"] * seconds / 3600.0
        # A linear decay run far enough goes to zero and then negative. The
        # floor stops the prediction becoming nonsense; the page says when it bit.
        power = max(power, lowest)
        v = speed_for_power(power, grade[i], max_speed=max_speed, **params)
        if v < min_speed:
            v = min_speed  # walking the steep bit rather than dividing by nothing
            crawled = True
        watts.append(power)
        speeds.append(v)
        flat.append(flat_equivalent_speed(power, **params))
        seconds += (distance[i + 1] - distance[i]) / v
        elapsed.append(seconds)

    for series in (watts, speeds, flat):
        series.append(series[-1])

    return {
        "distance": distance,
        "elevation": elevation,
        "elevation_smoothed": smoothed,
        "grade": grade,
        "elapsed": elapsed,
        "speed": speeds,
        "power": watts,
        "flat_speed": flat,
        "seconds": seconds,
        "floored": watts[-1] <= lowest * 1.0001,
        "crawled": crawled,
        "params": params,
    }
