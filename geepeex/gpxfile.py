"""Turning Strava activity streams into GPX, and naming the files they land in."""

import re
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

GPX_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<gpx version="1.1" creator="geepeex" xmlns="http://www.topografix.com/GPX/1/1" '
    'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1" '
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


def _data(streams, key):
    return streams.get(key, {}).get("data") or []


def _trkseg(activity, streams):
    """One activity's streams as <trkseg> lines.

    Heart rate, cadence and temperature go in Garmin's TrackPointExtension,
    which Strava documents reading on upload; power goes in a bare <power>
    extension, which is common practice but not in Strava's upload docs.
    """
    latlng = _data(streams, "latlng")
    if not latlng:
        raise NoGpsData(f"activity {activity.get('id')} has no latlng stream")

    altitude = _data(streams, "altitude")
    offsets = _data(streams, "time")
    sensors = [(tag, _data(streams, key)) for tag, key in
               (("gpxtpx:atemp", "temp"), ("gpxtpx:hr", "heartrate"), ("gpxtpx:cad", "cadence"))]
    watts = _data(streams, "watts")
    start = parse_strava_time(activity["start_date"])

    out = ["    <trkseg>\n"]
    for i, point in enumerate(latlng):
        lat, lon = point
        out.append(f'      <trkpt lat="{lat:.7f}" lon="{lon:.7f}">\n')
        if i < len(altitude) and altitude[i] is not None:
            out.append(f"        <ele>{altitude[i]:.1f}</ele>\n")
        if i < len(offsets):
            out.append(f"        <time>{_iso(start + timedelta(seconds=offsets[i]))}</time>\n")
        tpx = "".join(f"<{tag}>{values[i]:g}</{tag}>" for tag, values in sensors
                      if i < len(values) and values[i] is not None)
        power = f"<power>{watts[i]:g}</power>" if i < len(watts) and watts[i] is not None else ""
        if tpx or power:
            if tpx:
                tpx = f"<gpxtpx:TrackPointExtension>{tpx}</gpxtpx:TrackPointExtension>"
            out.append(f"        <extensions>{power}{tpx}</extensions>\n")
        out.append("      </trkpt>\n")
    out.append("    </trkseg>\n")
    return out


def _document(name, sport, start, segments, desc=""):
    name, sport = escape(name), escape(sport)
    meta = f"    <name>{name}</name>\n"
    if desc:
        meta += f"    <desc>{escape(desc)}</desc>\n"
    out = [GPX_HEADER, f"  <metadata>\n{meta}    <time>{_iso(start)}</time>\n  </metadata>\n",
           f"  <trk>\n    <name>{name}</name>\n    <type>{sport}</type>\n"]
    for segment in segments:
        out.extend(segment)
    out.append("  </trk>\n</gpx>\n")
    return "".join(out)


def build_gpx(activity, streams):
    """Render one activity's streams as a single-segment GPX track.

    `streams` is Strava's key_by_type response: {"latlng": {"data": [...]}, ...}.
    Altitude, time and the sensor streams are optional; latlng is not.
    """
    return _document(
        activity.get("name") or f"Activity {activity['id']}",
        activity.get("sport_type") or activity.get("type") or "",
        parse_strava_time(activity["start_date"]),
        [_trkseg(activity, streams)],
    )


def merge_gpx(parts, name, sport, desc=""):
    """Several activities as one track, one segment each.

    Segments stay apart rather than being joined, because the gap between two
    recordings was not ridden in a straight line. `parts` is a chronological
    list of (activity, streams).
    """
    return _document(name, sport, parse_strava_time(parts[0][0]["start_date"]),
                     [_trkseg(activity, streams) for activity, streams in parts], desc)


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


def read_metadata(path):
    """The name, description and sport a file declares, for uploading it."""
    import xml.etree.ElementTree as ET

    found = {}
    for element in ET.parse(path).getroot().iter():
        tag = _local(element.tag)
        if tag in ("name", "desc", "type") and tag not in found and element.text:
            found[tag] = element.text.strip()
    return found


def read_points(path):
    """All tracks in a file flattened into one point list, for a route that is
    logically continuous but split across segments."""
    return [point for track in read_tracks(path) for point in track]


def read_samples(path):
    """Segments of (lat, lon, elevation, time), keeping whatever is present.

    Elevation and time are None where the file omits them. Segments stay apart
    because speed across a paused recording is meaningless.
    """
    import xml.etree.ElementTree as ET

    root = ET.parse(path).getroot()
    segments = []
    for parent in root.iter():
        if _local(parent.tag) != "trkseg":
            continue
        points = []
        for child in parent:
            if _local(child.tag) != "trkpt" or not child.get("lat"):
                continue
            ele = time = None
            for field in child:
                if _local(field.tag) == "ele" and field.text:
                    ele = float(field.text)
                elif _local(field.tag) == "time" and field.text:
                    time = parse_strava_time(field.text.strip())
            points.append((float(child.get("lat")), float(child.get("lon")), ele, time))
        if len(points) >= 2:
            segments.append(points)
    return segments
