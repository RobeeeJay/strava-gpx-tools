"""Turning Strava activity streams into GPX, and naming the files they land in."""

import re
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

GPX_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<gpx version="1.1" creator="geepeex" xmlns="http://www.topografix.com/GPX/1/1" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:schemaLocation="http://www.topografix.com/GPX/1/1 '
    'http://www.topografix.com/GPX/1/1/gpx.xsd">\n'
)


class NoGpsData(Exception):
    """Raised for trainer rides, pool swims and anything else Strava recorded without a latlng stream."""


def parse_strava_time(value):
    # Strava stamps UTC times with a trailing Z, which datetime.fromisoformat
    # rejects before 3.11.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_gpx(activity, streams):
    """Render one activity's streams as a single-segment GPX track.

    `streams` is Strava's key_by_type response: {"latlng": {"data": [...]}, ...}.
    Altitude and time are optional; latlng is not.
    """
    latlng = streams.get("latlng", {}).get("data") or []
    if not latlng:
        raise NoGpsData(f"activity {activity.get('id')} has no latlng stream")

    altitude = streams.get("altitude", {}).get("data") or []
    offsets = streams.get("time", {}).get("data") or []
    start = parse_strava_time(activity["start_date"])

    name = escape(activity.get("name") or f"Activity {activity['id']}")
    sport = escape(activity.get("sport_type") or activity.get("type") or "")

    out = [GPX_HEADER]
    out.append(f"  <metadata>\n    <name>{name}</name>\n    <time>{_iso(start)}</time>\n  </metadata>\n")
    out.append(f"  <trk>\n    <name>{name}</name>\n    <type>{sport}</type>\n    <trkseg>\n")
    for i, point in enumerate(latlng):
        lat, lon = point
        out.append(f'      <trkpt lat="{lat:.7f}" lon="{lon:.7f}">\n')
        if i < len(altitude):
            out.append(f"        <ele>{altitude[i]:.1f}</ele>\n")
        if i < len(offsets):
            out.append(f"        <time>{_iso(start + timedelta(seconds=offsets[i]))}</time>\n")
        out.append("      </trkpt>\n")
    out.append("    </trkseg>\n  </trk>\n</gpx>\n")
    return "".join(out)


def slugify(text, limit=48):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:limit].rstrip("-") or "activity"


def filename_for(activity):
    """Date-led so the folder sorts chronologically; id-tailed so two rides
    on one day with one name cannot collide."""
    local = activity.get("start_date_local") or activity["start_date"]
    day = parse_strava_time(local).strftime("%Y-%m-%d")
    return f"{day}-{slugify(activity.get('name'))}-{activity['id']}.gpx"


# --- reading ----------------------------------------------------------------


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def read_tracks(path):
    """Every track and route in a file, as lists of (lat, lon).

    Segments are kept apart: a <trkseg> break is a real discontinuity (paused
    recording), and joining them would invent a straight line nobody rode.
    """
    import xml.etree.ElementTree as ET

    root = ET.parse(path).getroot()
    tracks = []
    for parent in root.iter():
        tag = _local(parent.tag)
        if tag not in ("trkseg", "rte"):
            continue
        point_tag = "trkpt" if tag == "trkseg" else "rtept"
        points = [
            (float(child.get("lat")), float(child.get("lon")))
            for child in parent
            if _local(child.tag) == point_tag and child.get("lat") and child.get("lon")
        ]
        if len(points) >= 2:
            tracks.append(points)
    return tracks


def read_points(path):
    """All tracks in a file flattened into one point list, for a route that is
    logically continuous but split across segments."""
    return [point for track in read_tracks(path) for point in track]
