import pytest

from geepeex.gpxfile import NoGpsData, build_gpx, filename_for, slugify
from geepeex.strava import filter_activities, matches

RIDE = {
    "id": 123,
    "name": "Morning Commute to Work",
    "sport_type": "Ride",
    "start_date": "2026-08-12T06:30:00Z",
    "start_date_local": "2026-08-12T07:30:00Z",
    "distance": 12400.0,
}
RUN = {
    "id": 456,
    "name": "Evening trail run",
    "sport_type": "Run",
    "start_date": "2026-08-13T17:00:00Z",
    "start_date_local": "2026-08-13T18:00:00Z",
    "distance": 8000.0,
}
STREAMS = {
    "latlng": {"data": [[51.5074, -0.1278], [51.5080, -0.1290], [51.5090, -0.1301]]},
    "altitude": {"data": [11.2, 13.8, 15.0]},
    "time": {"data": [0, 5, 11]},
}


def test_keyword_match_ignores_case_and_matches_substrings():
    assert matches(RIDE, keywords=["commute"])
    assert matches(RIDE, keywords=["COMMUTE"])
    assert not matches(RIDE, keywords=["swim"])


def test_several_keywords_are_an_or_not_an_and():
    # A rider asking for "commute" and "trail" wants both sets, not the
    # empty intersection of names containing each word.
    assert filter_activities([RIDE, RUN], keywords=["commute", "trail"]) == [RIDE, RUN]


def test_type_filter_keeps_only_the_named_sports():
    assert filter_activities([RIDE, RUN], types=["Run"]) == [RUN]
    assert filter_activities([RIDE, RUN], types=["run"]) == [RUN], "type match is case-insensitive"


def test_empty_filters_match_everything():
    assert filter_activities([RIDE, RUN]) == [RIDE, RUN]


def test_keyword_and_type_filters_both_have_to_pass():
    assert filter_activities([RIDE, RUN], keywords=["commute"], types=["Run"]) == []


def test_type_falls_back_to_the_legacy_field():
    # Strava deprecated `type` in favour of `sport_type`; old cached activities
    # and some endpoints still only carry the old one.
    legacy = {"id": 9, "name": "Old ride", "type": "Ride"}
    assert filter_activities([legacy], types=["Ride"]) == [legacy]


def test_gpx_has_one_trackpoint_per_latlng_sample():
    gpx = build_gpx(RIDE, STREAMS)
    assert gpx.count("<trkpt") == 3, gpx


def test_trackpoint_times_are_the_start_plus_the_time_stream_offset():
    gpx = build_gpx(RIDE, STREAMS)
    # time stream is seconds from start_date, so 11s past 06:30:00Z.
    assert "<time>2026-08-12T06:30:11Z</time>" in gpx, gpx


def test_elevation_comes_through_when_the_altitude_stream_is_present():
    assert "<ele>13.8</ele>" in build_gpx(RIDE, STREAMS)


def test_gpx_is_still_valid_without_altitude_or_time_streams():
    gpx = build_gpx(RIDE, {"latlng": STREAMS["latlng"]})
    assert gpx.count("<trkpt") == 3
    assert "<ele>" not in gpx and "<time>2026-08-12T06:30:00Z</time>" in gpx


def test_activity_without_a_latlng_stream_is_rejected_not_written_empty():
    # Trainer rides and pool swims return streams with no latlng; writing a
    # trackless GPX would look like a successful download.
    with pytest.raises(NoGpsData):
        build_gpx(RIDE, {"time": {"data": [0, 5]}})


def test_activity_name_is_xml_escaped():
    gpx = build_gpx(dict(RIDE, name="Ride & <fun>"), STREAMS)
    assert "Ride &amp; &lt;fun&gt;" in gpx
    assert "<fun>" not in gpx


def test_filename_is_date_led_and_id_tailed_so_same_day_rides_cannot_collide():
    assert filename_for(RIDE) == "2026-08-12-morning-commute-to-work-123.gpx"
    twin = dict(RIDE, id=124)
    assert filename_for(twin) != filename_for(RIDE)


def test_filename_uses_local_date_not_utc():
    # A 00:30 ride in a +03:00 zone happened on the 13th as far as the rider is
    # concerned, but is still the 12th in UTC. Strava stamps start_date_local
    # with a Z despite it being local time, so it parses to the wall clock.
    late = dict(RIDE, start_date="2026-08-12T21:30:00Z", start_date_local="2026-08-13T00:30:00Z")
    assert filename_for(late).startswith("2026-08-13"), filename_for(late)


def test_slugify_survives_a_name_with_nothing_usable_in_it():
    assert slugify("!!!") == "activity"
