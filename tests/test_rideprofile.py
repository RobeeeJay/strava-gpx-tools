import math
from datetime import datetime, timedelta, timezone

import pytest

from geepeex.gpxfile import read_samples
from geepeex.rideprofile import (DEFAULTS, G, NotEnoughData, analyse, fatigue,
                                 flat_equivalent_speed)

LAT, LON = 51.5, -0.1
M_PER_DEG_LON = 111195.0 * math.cos(math.radians(LAT))
START = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)


def speed_for(power, grade, **over):
    """The forward model: what speed this power produces on this gradient.

    analyse() inverts this, so simulating with it and checking what comes back
    tests the adjustment against a ground truth it never sees.
    """
    p = dict(DEFAULTS, **over)
    theta = math.atan(grade)
    target = power * p["efficiency"]
    lo, hi = 0.01, 30.0
    for _ in range(70):
        v = (lo + hi) / 2
        resist = v * (p["mass"] * G * (p["crr"] * math.cos(theta) + math.sin(theta))
                      + 0.5 * p["rho"] * p["cda"] * v * v)
        lo, hi = (v, hi) if resist < target else (lo, v)
    return (lo + hi) / 2


def simulate(total=20000.0, power=lambda d: 200.0, grade=lambda d: 0.0, step=2.0):
    distance = elevation = 0.0
    points, seconds = [], 0.0
    while distance < total:
        v = speed_for(power(distance), grade(distance))
        points.append((LAT, LON + distance / M_PER_DEG_LON, elevation,
                       START + timedelta(seconds=seconds)))
        distance += v * step
        elevation += v * step * grade(distance)
        seconds += step
    return [points]


def test_flat_equivalent_speed_inverts_the_forward_model():
    for watts in (120, 200, 300):
        v = speed_for(watts, 0.0)
        assert flat_equivalent_speed(watts, **DEFAULTS) == pytest.approx(v, rel=1e-3)


def test_the_physics_constants_still_give_familiar_road_speeds():
    # 200 W on a road bike at 85 kg all-in is about 33 km/h on the flat. If this
    # drifts, one of the defaults has been changed without meaning to.
    kmh = flat_equivalent_speed(200, **DEFAULTS) * 3.6
    assert 32 < kmh < 34.5, f"200 W gives {kmh:.1f} km/h"


def test_no_power_means_no_flat_equivalent_speed():
    assert flat_equivalent_speed(0.0, **DEFAULTS) == 0.0
    assert flat_equivalent_speed(-50.0, **DEFAULTS) == 0.0


def test_the_same_effort_uphill_and_on_the_flat_gives_the_same_adjusted_speed():
    # The whole point of the adjustment: a rider holding 200 W is having the
    # same day whether the road tilts up or not.
    flat = analyse(simulate(grade=lambda d: 0.0))
    climb = analyse(simulate(grade=lambda d: 0.04))
    mid = len(flat["flat_speed"]) // 2, len(climb["flat_speed"]) // 2
    assert climb["speed"][mid[1]] < flat["speed"][mid[0]] * 0.7, "the climb must be slower in reality"
    assert climb["flat_speed"][mid[1]] == pytest.approx(flat["flat_speed"][mid[0]], rel=0.02)


def test_a_ride_at_constant_power_shows_no_fade():
    signal = fatigue(analyse(simulate(grade=lambda d: 0.02 * math.sin(d / 800))))
    assert abs(signal["end_kmh"] - signal["start_kmh"]) < 0.3, signal


def test_a_declining_power_ride_recovers_the_fade_it_was_built_with():
    # 220 W falling to 180 W over 20 km, on rolling ground.
    result = analyse(simulate(
        power=lambda d: 220 - 40 * d / 20000,
        grade=lambda d: 0.03 * math.sin(2 * math.pi * d / 6000),
    ))
    signal = fatigue(result)
    expected = (flat_equivalent_speed(180, **DEFAULTS)
                - flat_equivalent_speed(220, **DEFAULTS)) * 3.6
    assert signal["end_kmh"] - signal["start_kmh"] == pytest.approx(expected, abs=0.25), signal


def test_a_climb_at_the_finish_is_not_reported_as_fatigue():
    # Raw speed collapses on the closing climb; the adjustment must not.
    result = analyse(simulate(total=20000, grade=lambda d: 0.0 if d < 15000 else 0.05))
    signal = fatigue(result)
    last, first = signal["quarters"][-1], signal["quarters"][0]
    assert last["actual_kmh"] < first["actual_kmh"] * 0.6, "the climb should be much slower"
    assert last["flat_kmh"] == pytest.approx(first["flat_kmh"], rel=0.03), signal


def test_gradient_is_right_at_the_very_end_of_a_sustained_climb():
    # A window that shrinks at the array end reports a shallower and shallower
    # grade, which read a steady 4% finish as 3% and invented a fade.
    result = analyse(simulate(total=6000, grade=lambda d: 0.04))
    grade = result["grade"]
    assert grade[-1] == pytest.approx(0.04, abs=0.002), grade[-5:]
    assert grade[0] == pytest.approx(0.04, abs=0.002), grade[:5]
    assert grade[len(grade) // 2] == pytest.approx(0.04, abs=0.002)


def test_gradient_is_clamped_so_an_altimeter_glitch_cannot_dominate():
    result = analyse(simulate(total=3000, grade=lambda d: 0.0), max_grade=0.05)
    assert all(abs(g) <= 0.05 for g in result["grade"])


def test_freewheeling_down_a_hill_is_not_counted_as_effort():
    # 20 km/h down an 8% grade needs negative power to hold: the rider is
    # coasting or braking, which says nothing about how hard they are working.
    # Simulating a rider *pedalling* downhill would not exercise this at all.
    coast = [(LAT, LON + (i * 11.1) / M_PER_DEG_LON, 500 - i * 11.1 * 0.08,
              START + timedelta(seconds=i * 2))
             for i in range(120)]
    result = analyse([coast])
    assert not any(result["propulsive"]), "coasting must not read as effort"
    assert all(w < 0 for w in result["power"][10:-10]), result["power"][:5]


def test_a_pedalled_ride_is_counted_as_effort():
    assert all(analyse(simulate(total=3000))["propulsive"])


def test_reported_average_speed_is_distance_over_time():
    # "Average speed" means the distance covered divided by the time it took,
    # which is not the mean of the speed samples once sampling is uneven.
    result = analyse(simulate(total=8000, power=lambda d: 120 if d < 4000 else 320))
    reported = fatigue(result, buckets=1)["quarters"][0]["actual_kmh"]
    truth = result["distance"][-1] / result["elapsed"][-1] * 3.6
    assert reported == pytest.approx(truth, rel=0.02), f"{reported:.2f} vs {truth:.2f}"


def test_each_bucket_reports_its_own_stretch_of_road():
    result = analyse(simulate(total=8000, power=lambda d: 120 if d < 4000 else 320))
    slow, fast = fatigue(result, buckets=2)["quarters"]
    assert fast["actual_kmh"] > slow["actual_kmh"] * 1.2, (slow, fast)


def test_a_file_without_timestamps_says_so_rather_than_dividing_by_zero(tmp_path):
    path = tmp_path / "no-time.gpx"
    path.write_text(
        '<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
        '<trkpt lat="51.5" lon="-0.1"><ele>10</ele></trkpt>'
        '<trkpt lat="51.5" lon="-0.09"><ele>12</ele></trkpt>'
        "</trkseg></trk></gpx>")
    with pytest.raises(NotEnoughData, match="timestamps"):
        analyse(read_samples(str(path)))


def test_reading_samples_keeps_elevation_and_time(tmp_path):
    path = tmp_path / "one.gpx"
    path.write_text(
        '<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
        '<trkpt lat="51.5" lon="-0.1"><ele>10.5</ele><time>2026-08-30T08:00:00Z</time></trkpt>'
        '<trkpt lat="51.5" lon="-0.09"><ele>12.5</ele><time>2026-08-30T08:00:30Z</time></trkpt>'
        "</trkseg></trk></gpx>")
    (segment,) = read_samples(str(path))
    assert segment[0][2] == 10.5
    assert (segment[1][3] - segment[0][3]).total_seconds() == 30
