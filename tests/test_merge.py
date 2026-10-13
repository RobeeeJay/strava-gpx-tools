import json

import pytest

from geepeex.gpxfile import build_gpx, merge_gpx, read_metadata, read_samples
from geepeex.merge import (CannotMerge, check_mergeable, find_archive, load_archive,
                           parse_selection, save_archive)
from geepeex.strava import UploadFailed, has_scope, upload_outcome

# Shapes as returned by /activities/{id} and its streams with key_by_type=true;
# watts carries a null where the power meter dropped out, as Strava sends it.
OUT = {
    "id": 101, "name": "Out to the coast", "sport_type": "Ride", "description": "windy",
    "start_date": "2026-09-01T08:00:00Z", "start_date_local": "2026-09-01T09:00:00Z",
}
BACK = {
    "id": 102, "name": "Back home", "sport_type": "Ride", "description": "",
    "start_date": "2026-09-01T10:00:00Z", "start_date_local": "2026-09-01T11:00:00Z",
}
OUT_STREAMS = {
    "latlng": {"data": [[50.59, -1.21], [50.60, -1.22], [50.61, -1.23]]},
    "altitude": {"data": [81.6, 85.0, 90.2]},
    "time": {"data": [0, 600, 1200]},
    "heartrate": {"data": [79, 131, 140]},
    "cadence": {"data": [0, 85, 88]},
    "watts": {"data": [0, None, 210]},
    "temp": {"data": [14, 14, 15]},
}
BACK_STREAMS = {
    "latlng": {"data": [[50.61, -1.23], [50.60, -1.22]]},
    "time": {"data": [0, 900]},
}


def test_selection_accepts_single_numbers_and_ranges_one_based():
    assert parse_selection("1, 3-4", 5) == [0, 2, 3]


def test_selection_outside_the_listed_activities_is_refused():
    with pytest.raises(CannotMerge, match="outside 1-5"):
        parse_selection("4-6", 5)


def test_blank_selection_is_refused_rather_than_merging_nothing():
    with pytest.raises(CannotMerge, match="nothing selected"):
        parse_selection(" , ", 5)


def test_merge_is_put_in_time_order_whatever_order_it_was_picked_in():
    ordered = check_mergeable([(BACK, BACK_STREAMS), (OUT, OUT_STREAMS)])
    assert [a["id"] for a, _ in ordered] == [101, 102]


def test_overlapping_activities_are_refused():
    # The outbound ride lasts 20 minutes; a return starting 10 minutes in is
    # either a duplicate recording or a merge that would run backwards.
    early = dict(BACK, start_date="2026-09-01T08:10:00Z")
    with pytest.raises(CannotMerge, match="still running"):
        check_mergeable([(OUT, OUT_STREAMS), (early, BACK_STREAMS)])


def test_activity_without_a_time_stream_is_refused_because_strava_would_reject_it():
    with pytest.raises(CannotMerge, match="no time stream"):
        check_mergeable([(OUT, OUT_STREAMS), (BACK, {"latlng": BACK_STREAMS["latlng"]})])


def test_activity_without_gps_cannot_be_merged():
    with pytest.raises(CannotMerge, match="no GPS track"):
        check_mergeable([(OUT, OUT_STREAMS), (BACK, {"time": BACK_STREAMS["time"]})])


def test_merged_file_keeps_each_activity_as_its_own_segment(tmp_path):
    # Joining the segments would draw a straight line across the gap between
    # recordings, which nobody rode.
    path = tmp_path / "merged.gpx"
    path.write_text(merge_gpx([(OUT, OUT_STREAMS), (BACK, BACK_STREAMS)], "Coast", "Ride"))
    segments = read_samples(path)
    assert [len(s) for s in segments] == [3, 2], segments
    assert segments[1][-1][3].isoformat() == "2026-09-01T10:15:00+00:00", segments[1][-1]


def test_merged_file_carries_the_name_description_and_sport_for_upload(tmp_path):
    path = tmp_path / "merged.gpx"
    path.write_text(merge_gpx([(OUT, OUT_STREAMS), (BACK, BACK_STREAMS)],
                              "Coast & back", "Ride", "windy <both> ways"))
    assert read_metadata(path) == {"name": "Coast & back", "desc": "windy <both> ways", "type": "Ride"}


def test_sensor_streams_are_written_as_trackpoint_extensions():
    gpx = build_gpx(OUT, OUT_STREAMS)
    assert ("<extensions><power>210</power><gpxtpx:TrackPointExtension><gpxtpx:atemp>15</gpxtpx:atemp>"
            "<gpxtpx:hr>140</gpxtpx:hr><gpxtpx:cad>88</gpxtpx:cad></gpxtpx:TrackPointExtension>"
            "</extensions>") in gpx, gpx


def test_a_power_dropout_omits_power_for_that_point_instead_of_writing_none():
    gpx = build_gpx(OUT, OUT_STREAMS)
    assert "None" not in gpx
    assert gpx.count("<power>") == 2, gpx


def test_archive_keeps_everything_the_api_returned_and_a_gpx_beside_it(tmp_path):
    snapshot = {"activity": OUT, "streams": OUT_STREAMS,
                "comments": [{"text": "nice"}], "kudos": [{"firstname": "A"}]}
    path = save_archive(str(tmp_path), snapshot)
    kept = load_archive(path)
    assert {k: kept[k] for k in snapshot} == json.loads(json.dumps(snapshot))
    assert (tmp_path / "2026-09-01-out-to-the-coast-101.gpx").exists()


def test_archive_of_an_activity_without_gps_still_keeps_the_json(tmp_path):
    path = save_archive(str(tmp_path), {"activity": BACK, "streams": {}, "comments": [], "kudos": []})
    assert load_archive(path)["activity"]["id"] == 102
    assert not list(tmp_path.glob("*.gpx"))


def test_finding_an_archive_does_not_match_an_id_that_merely_ends_the_same(tmp_path):
    save_archive(str(tmp_path), {"activity": dict(OUT, id=1101), "streams": OUT_STREAMS,
                                 "comments": [], "kudos": []})
    assert find_archive(str(tmp_path), 101) is None
    assert find_archive(str(tmp_path), 1101)


def test_upload_still_processing_has_no_activity_yet():
    # Shape from Strava's upload docs.
    assert upload_outcome({"id": 1, "error": None, "status": "Your activity is still being processed.",
                           "activity_id": None}) is None


def test_finished_upload_gives_the_new_activity():
    assert upload_outcome({"id": 1, "error": None, "status": "Your activity is ready.",
                           "activity_id": 555}) == 555


def test_duplicate_upload_names_the_activity_still_on_strava():
    # A duplicate means an original was not deleted; the error should say which.
    with pytest.raises(UploadFailed, match="strava.com/activities/21234316"):
        upload_outcome({"id": 1, "error": "Test_Walk.gpx duplicate of activity 21234316",
                        "status": "There was an error processing your activity.", "activity_id": None})


def test_scope_is_read_whether_strava_separated_it_with_spaces_or_commas():
    assert has_scope({"scope": "activity:read_all read activity:write"}, "activity:read_all,activity:write")
    assert has_scope({"scope": "read,activity:write,activity:read_all"}, "activity:read_all,activity:write")
    assert not has_scope({"scope": "activity:read_all read"}, "activity:read_all,activity:write")
    assert not has_scope({}, "activity:write")
