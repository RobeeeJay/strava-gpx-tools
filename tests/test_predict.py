import math
from datetime import datetime, timedelta, timezone

import pytest

from geepeex.predict import NotEnoughRides, fitness, predict, ride_fitness
from geepeex.rideprofile import DEFAULTS, analyse, speed_for_power
from test_rideprofile import LAT, LON, M_PER_DEG_LON, speed_for

TODAY = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
PARAMS = dict(DEFAULTS)


def simulate_ride(total=20000.0, grade=lambda d: 0.0, start_watts=230.0,
                  decay=-18.0, when=TODAY, step=2.0):
    """A rider holding a known, decaying power. `speed_for` is the independent
    solver in test_rideprofile, so the analysis is not being checked against
    the same code that generated the data."""
    distance = elevation = seconds = 0.0
    points = []
    while distance < total:
        watts = start_watts + decay * seconds / 3600.0
        v = max(speed_for(watts, grade(distance)), 1.0)
        points.append((LAT, LON + distance / M_PER_DEG_LON, elevation,
                       when + timedelta(seconds=seconds)))
        distance += v * step
        elevation += v * step * grade(distance)
        seconds += step
    return [points], seconds


def fit_of(**kw):
    segments, truth = simulate_ride(**kw)
    result = ride_fitness(analyse(segments))
    return result, truth


def test_one_ride_recovers_the_power_it_was_ridden_at():
    fit, _ = fit_of(start_watts=230.0, decay=-18.0, total=25000)
    assert fit["start_watts"] == pytest.approx(230, abs=4), fit
    assert fit["decay_watts_per_hour"] == pytest.approx(-18, abs=2), fit


def test_a_ride_at_steady_power_shows_no_decay():
    fit, _ = fit_of(start_watts=200.0, decay=0.0, total=20000)
    assert abs(fit["decay_watts_per_hour"]) < 2, fit


def test_recent_rides_count_for_more_than_old_ones():
    old = {"date": TODAY - timedelta(days=84), "start_watts": 150.0,
           "decay_watts_per_hour": -10.0, "hours": 2.0}
    new = {"date": TODAY, "start_watts": 250.0,
           "decay_watts_per_hour": -10.0, "hours": 2.0}
    combined = fitness([dict(old), dict(new)], half_life_days=42)["start_watts"]
    assert 200 < combined < 250, combined
    assert combined > 200, "the recent ride has to pull the average above the midpoint"


def test_a_shorter_half_life_leans_harder_on_the_newest_ride():
    rides = [{"date": TODAY - timedelta(days=60), "start_watts": 150.0,
              "decay_watts_per_hour": -10.0, "hours": 2.0},
             {"date": TODAY, "start_watts": 250.0,
              "decay_watts_per_hour": -10.0, "hours": 2.0}]
    slow = fitness([dict(r) for r in rides], half_life_days=90)["start_watts"]
    fast = fitness([dict(r) for r in rides], half_life_days=7)["start_watts"]
    assert fast > slow, f"half-life 7 gave {fast:.0f} W, half-life 90 gave {slow:.0f} W"


def test_a_long_ride_counts_for_more_than_a_short_one_the_same_day():
    # The fade is the quantity of interest and a twenty-minute ride cannot show
    # one, so duration is part of the weight.
    rides = [{"date": TODAY, "start_watts": 150.0, "decay_watts_per_hour": -30.0, "hours": 0.4},
             {"date": TODAY, "start_watts": 250.0, "decay_watts_per_hour": -5.0, "hours": 4.0}]
    out = fitness(rides)
    assert out["start_watts"] > 230, out
    assert out["decay_watts_per_hour"] > -10, out


def test_no_usable_rides_is_an_error_not_a_guess():
    with pytest.raises(NotEnoughRides):
        fitness([])


def test_the_predicted_time_matches_what_the_rider_actually_took():
    # The headline check: train on rides the predictor sees, then predict a
    # route it has never seen and compare against that same rider's real time.
    history = []
    for weeks, distance, shape in ((4, 20000, 0.02), (3, 30000, 0.03), (1, 25000, 0.0)):
        when = TODAY - timedelta(weeks=weeks)
        segments, _ = simulate_ride(total=distance, when=when,
                                    grade=lambda d, s=shape: s * math.sin(d / 900))
        fit = ride_fitness(analyse(segments))
        fit["date"] = when
        history.append(fit)

    def route_grade(d):
        return 0.05 if 8000 < d < 12000 else -0.005

    unseen, truth = simulate_ride(total=18000, grade=route_grade, when=TODAY)
    points = [(lat, lon) for lat, lon, _, _ in unseen[0]]
    elevation = [ele for _, _, ele, _ in unseen[0]]

    forecast = predict(points, elevation, fitness(history), PARAMS)
    assert forecast["seconds"] == pytest.approx(truth, rel=0.04), (
        f"predicted {forecast['seconds'] / 60:.1f} min, actual {truth / 60:.1f} min")


def test_the_prediction_slows_down_where_the_route_goes_up():
    flat_points, _ = simulate_ride(total=6000, grade=lambda d: 0.0)
    fit = {"start_watts": 200.0, "decay_watts_per_hour": 0.0}
    points = [(lat, lon) for lat, lon, _, _ in flat_points[0]]
    level = predict(points, [0.0] * len(points), fit, PARAMS)
    climbing = predict(points, [i * 0.5 for i in range(len(points))], fit, PARAMS)
    assert climbing["seconds"] > level["seconds"] * 1.5, (level["seconds"], climbing["seconds"])


def test_the_modelled_power_is_floored_and_the_floor_is_reported():
    # A linear decay run far enough goes negative; the prediction must not.
    flat_points, _ = simulate_ride(total=8000, grade=lambda d: 0.0)
    points = [(lat, lon) for lat, lon, _, _ in flat_points[0]]
    forecast = predict(points, [0.0] * len(points),
                       {"start_watts": 200.0, "decay_watts_per_hour": -400.0}, PARAMS)
    assert forecast["floored"] is True
    assert min(forecast["power"]) == pytest.approx(100.0), min(forecast["power"])


def test_speed_on_a_descent_is_capped_rather_than_unbounded():
    # The model has no notion of braking, cornering or self-preservation.
    fast = speed_for_power(250, -0.12, max_speed=18.0, **PARAMS)
    assert fast == pytest.approx(18.0, abs=0.01), fast


def test_no_power_on_a_climb_means_no_progress():
    assert speed_for_power(0.0, 0.05, **PARAMS) == 0.0


def test_power_decays_with_time_on_the_bike_not_with_distance_covered():
    # An hour of grinding uphill tires a rider as much as an hour of rolling
    # along; it just covers less ground. Decaying with distance would have the
    # slow route finish fresher than the fast one.
    flat_points, _ = simulate_ride(total=10000, grade=lambda d: 0.0)
    points = [(lat, lon) for lat, lon, _, _ in flat_points[0]]
    fit = {"start_watts": 220.0, "decay_watts_per_hour": -40.0}

    level = predict(points, [0.0] * len(points), fit, PARAMS)
    steep = predict(points, [i * 0.6 for i in range(len(points))], fit, PARAMS)

    assert steep["seconds"] > level["seconds"], "the climb must take longer"
    assert steep["power"][-1] < level["power"][-1] - 5, (
        f"after {steep['seconds'] / 60:.0f} min climbing the rider should be more "
        f"faded than after {level['seconds'] / 60:.0f} min on the flat, but the "
        f"modelled power finished at {steep['power'][-1]:.0f} W vs {level['power'][-1]:.0f} W")


def test_freewheeling_does_not_drag_down_the_fitted_power():
    # A ride that finishes with a long descent is not a ride that finished
    # weak. Fitting over every sample would read the coasting as a collapse.
    segments, _ = simulate_ride(total=12000, grade=lambda d: 0.0,
                                start_watts=230.0, decay=0.0)
    ridden = segments[0]
    last_lon = ridden[-1][1]
    last_time = ridden[-1][3]
    coast = [(LAT, last_lon + (i * 12.0) / M_PER_DEG_LON, -i * 12.0 * 0.08,
              last_time + timedelta(seconds=i * 2))
             for i in range(1, 400)]

    with_descent = ride_fitness(analyse([ridden + coast]))
    assert with_descent["start_watts"] == pytest.approx(230, abs=6), with_descent
    assert with_descent["decay_watts_per_hour"] > -15, (
        f"a closing descent read as a {with_descent['decay_watts_per_hour']:.0f} W/hour fade")
