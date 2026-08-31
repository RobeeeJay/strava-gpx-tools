import math

from geepeex.coverage import OPPOSITE, SAME, UNRIDDEN, classify, runs, segment_lengths
from geepeex.gpxfile import read_tracks

# 51.5N, so a degree of longitude is ~69.3 km and a degree of latitude ~111.3 km.
LAT = 51.5
M_PER_DEG_LAT = 111195.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(LAT))


def east(metres):
    return -0.1 + metres / M_PER_DEG_LON


def north(metres):
    return LAT + metres / M_PER_DEG_LAT


def line_east(start, end, step=50):
    """A due-east track from `start` to `end` metres along the same latitude."""
    n = int(abs(end - start) // step)
    direction = 1 if end >= start else -1
    return [(LAT, east(start + direction * i * step)) for i in range(n + 1)]


ROUTE = line_east(0, 1000)


def labels_only(main, training, **kw):
    return classify(main, training, **kw)[1]


def test_route_with_no_training_is_entirely_unridden():
    assert set(labels_only(ROUTE, [])) == {UNRIDDEN}


def test_training_along_the_route_in_the_same_direction_is_same():
    assert set(labels_only(ROUTE, [line_east(0, 1000)])) == {SAME}


def test_training_along_the_route_backwards_is_opposite():
    assert set(labels_only(ROUTE, [line_east(1000, 0)])) == {OPPOSITE}


def test_same_direction_outranks_opposite_when_both_were_ridden():
    # An out-and-back training ride covers the route each way; the route's own
    # direction is the one worth reporting.
    both = labels_only(ROUTE, [line_east(0, 1000), line_east(1000, 0)])
    assert set(both) == {SAME}


def test_partial_training_leaves_the_rest_unridden():
    labels = labels_only(ROUTE, [line_east(0, 400)])
    assert labels[0] == SAME, "the covered start should be green"
    assert labels[-1] == UNRIDDEN, "the uncovered end should be orange"
    assert set(labels) == {SAME, UNRIDDEN}


def test_a_track_offset_within_tolerance_still_counts():
    # 15 m of GPS drift beside the route is the same road, not a different one.
    drifted = [(north(15), lon) for _, lon in line_east(0, 1000)]
    assert set(labels_only(ROUTE, [drifted], tolerance=25)) == {SAME}


def test_a_track_beyond_tolerance_does_not_count():
    parallel = [(north(120), lon) for _, lon in line_east(0, 1000)]
    assert set(labels_only(ROUTE, [parallel], tolerance=25)) == {UNRIDDEN}


def test_a_training_ride_crossing_the_route_does_not_mark_it_ridden():
    # Without the angle test every junction a training ride passed through
    # would paint the route as covered.
    crossing = [(north(-300), east(500)), (north(300), east(500))]
    assert set(labels_only(ROUTE, [crossing])) == {UNRIDDEN}


def test_a_paused_recording_does_not_paint_the_jump_across_the_route():
    # A track that stops 2 km west and resumes 2 km east has a straight line
    # through the whole route that was never ridden.
    jump = [(LAT, east(-2000)), (LAT, east(3000))]
    assert set(labels_only(ROUTE, [jump], max_gap=200)) == {UNRIDDEN}


def test_raising_max_gap_lets_the_jump_through():
    # Guards the previous test against passing for the wrong reason: the jump
    # is discarded by the gap rule, not by geometry.
    jump = [(LAT, east(-2000)), (LAT, east(3000))]
    assert set(labels_only(ROUTE, [jump], max_gap=10000)) == {SAME}


def test_segments_are_no_longer_than_the_resolution():
    points, labels = classify(ROUTE, [], resolution=15)
    lengths = segment_lengths(points)
    assert len(lengths) == len(labels)
    assert max(lengths) <= 15.5, f"longest segment {max(lengths):.1f} m"


def test_route_length_survives_densifying():
    points, _ = classify(ROUTE, [], resolution=15)
    assert abs(sum(segment_lengths(points)) - 1000) < 5


def test_runs_collapse_neighbouring_segments_of_one_colour():
    points = [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)]
    collapsed = runs(points, [SAME, SAME, UNRIDDEN, UNRIDDEN])
    assert [label for label, _ in collapsed] == [SAME, UNRIDDEN]
    assert collapsed[0][1] == points[:3], "a run has to include the point it ends on"
    assert collapsed[1][1] == points[2:], "runs share the joining point so the line has no hole"


def test_reading_a_gpx_keeps_segments_apart(tmp_path):
    # Two <trkseg> in one file is a paused recording, not one continuous line.
    path = tmp_path / "two.gpx"
    path.write_text(
        '<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk>'
        '<trkseg><trkpt lat="51.5" lon="-0.1"/><trkpt lat="51.6" lon="-0.1"/></trkseg>'
        '<trkseg><trkpt lat="52.5" lon="-0.1"/><trkpt lat="52.6" lon="-0.1"/></trkseg>'
        "</trk></gpx>"
    )
    tracks = read_tracks(str(path))
    assert len(tracks) == 2, tracks
    assert tracks[0][0] == (51.5, -0.1)
