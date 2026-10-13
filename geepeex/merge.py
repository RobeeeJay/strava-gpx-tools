"""Joining several Strava activities into one upload, and archiving what
replacing them on Strava would otherwise lose."""

import glob
import json
import os
from datetime import datetime, timedelta, timezone

from .gpxfile import NoGpsData, build_gpx, filename_for, parse_strava_time


class CannotMerge(Exception):
    pass


def parse_selection(text, count):
    """1-based "1,3-5" into sorted 0-based indices."""
    chosen = set()
    for part in (p.strip() for p in text.split(",")):
        if not part:
            continue
        low, _, high = part.partition("-")
        try:
            low, high = int(low), int(high or low)
        except ValueError:
            raise CannotMerge(f"not a number or range: {part!r}") from None
        if not 1 <= low <= high <= count:
            raise CannotMerge(f"{part!r} is outside 1-{count}")
        chosen.update(range(low - 1, high))
    if not chosen:
        raise CannotMerge("nothing selected")
    return sorted(chosen)


def _span(activity, streams):
    offsets = streams.get("time", {}).get("data") or []
    start = parse_strava_time(activity["start_date"])
    return start, start + timedelta(seconds=offsets[-1] if offsets else 0)


def check_mergeable(parts):
    """Refuse anything Strava would reject or that would merge into nonsense.

    Strava requires a time on every trackpoint, and two activities overlapping
    in time are either duplicates or a merge that runs backwards.
    """
    if len(parts) < 2:
        raise CannotMerge("pick at least two activities to merge")
    for activity, streams in parts:
        label = f"{activity.get('name')!r} ({activity['id']})"
        if not streams.get("latlng", {}).get("data"):
            raise CannotMerge(f"{label} has no GPS track")
        if not streams.get("time", {}).get("data"):
            raise CannotMerge(f"{label} has no time stream, which Strava requires for an upload")
    ordered = sorted(parts, key=lambda part: _span(*part)[0])
    for (a, a_streams), (b, b_streams) in zip(ordered, ordered[1:]):
        if _span(b, b_streams)[0] < _span(a, a_streams)[1]:
            raise CannotMerge(
                f"{a.get('name')!r} ({a['id']}) is still running when "
                f"{b.get('name')!r} ({b['id']}) starts"
            )
    return ordered


def find_archive(folder, activity_id):
    found = glob.glob(os.path.join(glob.escape(folder), f"*-{activity_id}.json"))
    return found[0] if found else None


def save_archive(folder, snapshot):
    """Write the snapshot as JSON and, where there is a track, as GPX too.

    The JSON is the archive: everything the API returned, so nothing is lost to
    the GPX format. The GPX beside it is for re-uploading by hand.
    """
    os.makedirs(folder, exist_ok=True)
    stem = os.path.join(folder, filename_for(snapshot["activity"])[: -len(".gpx")])
    snapshot = dict(snapshot, archived_at=datetime.now(timezone.utc).isoformat())
    try:
        gpx = build_gpx(snapshot["activity"], snapshot["streams"])
    except NoGpsData:
        gpx = None
    if gpx:
        with open(stem + ".gpx", "w") as fh:
            fh.write(gpx)
    # JSON last: its presence is what marks the archive complete.
    with open(stem + ".json", "w") as fh:
        json.dump(snapshot, fh, indent=1)
    return stem + ".json"


def load_archive(path):
    with open(path) as fh:
        return json.load(fh)
