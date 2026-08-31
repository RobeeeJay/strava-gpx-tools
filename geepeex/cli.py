"""`gpx` command line: subcommands that operate on GPX tracks."""

import argparse
import glob
import os
import sys
from collections import Counter

from . import charts, coverage, mapview, rideprofile, strava
from .gpxfile import (NoGpsData, build_gpx, filename_for, parse_strava_time,
                      read_points, read_samples, read_tracks)

DEFAULT_OUT = "gpx"


def _prompt(question, default=""):
    try:
        answer = input(question).strip()
    except EOFError:
        return default
    return answer or default


def _split(value):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _confirm(question):
    return _prompt(f"{question} [y/N] ").lower() in ("y", "yes")


def _ensure_credentials(reauth=False):
    config = strava.load_config()
    have_app = config.get("client_id") and config.get("client_secret")

    if not have_app:
        print(
            "No Strava app credentials yet.\n"
            "Create one at https://www.strava.com/settings/api — set the\n"
            f"Authorization Callback Domain to `localhost` (we listen on port {strava.CALLBACK_PORT}).\n"
        )
        config["client_id"] = _prompt("Client ID: ")
        config["client_secret"] = _prompt("Client Secret: ")
        if not config["client_id"] or not config["client_secret"]:
            raise strava.StravaError("client id and secret are both required")
        reauth = True

    if reauth or not config.get("refresh_token"):
        config.update(strava.authorize(config["client_id"], config["client_secret"]))
        config.pop("athlete", None)  # bulky and never read back

    strava.save_config(config)
    print(f"Credentials stored in {strava.CONFIG_PATH}")
    return config


def _print_table(activities):
    for i, activity in enumerate(activities, 1):
        day = parse_strava_time(
            activity.get("start_date_local") or activity["start_date"]
        ).strftime("%Y-%m-%d")
        km = (activity.get("distance") or 0) / 1000
        print(
            f"{i:4}. {day}  {strava.sport_of(activity):<16}"
            f"{km:8.1f} km  {activity.get('name', '')}"
        )


def cmd_strava(args):
    config = _ensure_credentials(reauth=args.reauth)
    client = strava.StravaClient(config, on_config_change=strava.save_config)

    after = int(parse_strava_time(args.after + "T00:00:00Z").timestamp()) if args.after else None
    before = int(parse_strava_time(args.before + "T00:00:00Z").timestamp()) if args.before else None

    print("Fetching activity list...")
    activities = list(client.activities(after=after, before=before))
    if not activities:
        print("No activities in range.")
        return 0
    print(f"{len(activities)} activities on the account.")

    keywords = args.keywords
    types = args.types
    if not args.yes and keywords is None and types is None:
        # Filtering is local, so we can show the user what is actually available
        # before asking what they want out of it.
        seen = Counter(strava.sport_of(a) for a in activities)
        print("\nTypes present: " + ", ".join(f"{k} ({v})" for k, v in seen.most_common()))
        keywords = _split(_prompt("\nKeywords in activity name (comma separated, blank = all): "))
        types = _split(_prompt("Activity types (comma separated, blank = all): "))

    matched = strava.filter_activities(activities, keywords or [], types or [])
    if not matched:
        print("Nothing matched those filters.")
        return 1

    print(f"\n{len(matched)} matching activities:\n")
    _print_table(matched)

    out_dir = args.out
    print(f"\nDestination: {os.path.abspath(out_dir)}")
    if not args.yes and not _confirm(f"Download {len(matched)} GPX tracks?"):
        print("Cancelled. Nothing downloaded.")
        return 1

    os.makedirs(out_dir, exist_ok=True)
    written = skipped = no_gps = 0
    for activity in matched:
        path = os.path.join(out_dir, filename_for(activity))
        if os.path.exists(path):
            skipped += 1
            continue
        try:
            gpx = build_gpx(activity, client.streams(activity["id"]))
        except NoGpsData:
            no_gps += 1
            print(f"  no GPS data: {activity.get('name')}")
            continue
        except strava.RateLimited as exc:
            print(f"\n{exc}")
            print(f"Downloaded {written} before stopping.")
            return 2
        with open(path, "w") as fh:
            fh.write(gpx)
        written += 1
        print(f"  {path}")

    print(
        f"\nDone: {written} written, {skipped} already present, {no_gps} without GPS."
        + (f"\nRate limit usage: {client.usage}" if client.usage else "")
    )
    return 0


def _expand(patterns, exclude=()):
    """Take already-expanded shell arguments or a quoted glob, either way."""
    skip = {os.path.realpath(p) for p in exclude}
    paths = []
    for pattern in patterns:
        found = [pattern] if os.path.isfile(pattern) else sorted(glob.glob(pattern, recursive=True))
        for path in found:
            real = os.path.realpath(path)
            if real in skip:
                continue
            skip.add(real)
            paths.append(path)
    return paths


def _decimate(tracks, budget=60000):
    """Thin the display-only training layer. It exists to make a colouring look
    right or wrong at a glance; it is not what anything is measured from."""
    total = sum(len(t) for t in tracks)
    step = max(1, -(-total // budget)) if total else 1
    return [t[::step] + [t[-1]] for t in tracks if len(t) >= 2] if step > 1 else tracks


def cmd_coverage(args):
    if not os.path.isfile(args.main):
        raise strava.StravaError(f"main route not found: {args.main}")
    main_points = read_points(args.main)
    if len(main_points) < 2:
        raise strava.StravaError(f"{args.main} has no track points")

    # A training glob such as gpx/*.gpx usually contains the main route itself,
    # which would otherwise colour the whole thing green.
    training_files = _expand(args.training, exclude=[args.main])
    if not training_files:
        raise strava.StravaError("no training files matched")

    training = []
    for path in training_files:
        training.extend(read_tracks(path))
    print(f"{len(training_files)} training files, {len(training)} tracks, "
          f"{sum(len(t) for t in training)} points")

    points, labels = coverage.classify(
        main_points,
        training,
        tolerance=args.tolerance,
        angle=args.angle,
        resolution=args.resolution,
        max_gap=args.max_gap,
    )
    lengths = coverage.segment_lengths(points)
    stats = {coverage.UNRIDDEN: 0.0, coverage.OPPOSITE: 0.0, coverage.SAME: 0.0}
    for label, length in zip(labels, lengths):
        stats[label] += length

    total = sum(stats.values()) or 1
    print(f"\nMain route {total / 1000:.1f} km")
    for label in (coverage.SAME, coverage.OPPOSITE, coverage.UNRIDDEN):
        print(f"  {mapview.LABELS[label]:<26}{stats[label] / 1000:8.1f} km"
              f"{100 * stats[label] / total:7.0f}%")

    note = (f"{os.path.basename(args.main)} against {len(training_files)} training files. "
            f"Tolerance {args.tolerance:g} m, direction within {args.angle:g} degrees.")
    mapview.render(
        args.out,
        coverage.runs(points, labels),
        stats,
        title=args.title or os.path.splitext(os.path.basename(args.main))[0],
        training=() if args.no_training_layer else _decimate(training),
        note=note,
    )
    print(f"\nWrote {os.path.abspath(args.out)}")
    return 0


def _stride(values, count):
    if len(values) <= count:
        return values
    step = len(values) / count
    return [values[min(len(values) - 1, int(i * step))] for i in range(count)] + [values[-1]]


def cmd_profile(args):
    if not os.path.isfile(args.gpx):
        raise strava.StravaError(f"not found: {args.gpx}")
    segments = read_samples(args.gpx)
    result = rideprofile.analyse(
        segments,
        smoothing=args.smoothing,
        window=args.window,
        mass=args.mass,
        cda=args.cda,
        crr=args.crr,
    )
    signal = rideprofile.fatigue(result)

    n = args.points
    km = [d / 1000 for d in _stride(result["distance"], n)]
    x_max = km[-1]
    ele = _stride(result["elevation_smoothed"], n)
    speed = [v * 3.6 for v in _stride(result["speed"], n)]
    flat = [v * 3.6 for v in _stride(result["flat_speed"], n)]
    grade = [g * 100 for g in _stride(result["grade"], n)]
    elapsed = _stride(result["elapsed"], n)

    # The fit is drawn across the same x samples as the data it explains, so the
    # crosshair can read both from one index.
    (x0, y0), (x1, y1) = signal["trend"]
    trend = [y0 + (y1 - y0) * (d * 1000 - x0) / ((x1 - x0) or 1) for d in km]

    geometry = {"w": charts.W, "left": charts.LEFT, "right": charts.RIGHT}
    geom = {}
    blocks = []
    for spec in (
        ("elevation", "Elevation", "metres", [("Elevation", ele, 0)], True),
        ("speed", "Speed", "km/h, measured over %g m" % args.window,
         [("Speed", speed, 0)], False),
        ("flat", "Gradient-adjusted speed", "km/h the same effort would give on the flat",
         [("Flat-equivalent", flat, 0), ("Fitted trend", trend, 1)], False),
    ):
        chart_id, title, unit, series, fill = spec
        html, part = charts.line_chart(chart_id, title, unit, km, series, x_max, fill=fill)
        blocks.append(html)
        geom.update(part)

    change = signal["end_kmh"] - signal["start_kmh"]
    percent = 100 * change / signal["start_kmh"] if signal["start_kmh"] else 0.0
    climb = sum(max(0.0, b - a) for a, b in zip(result["elevation_smoothed"],
                                                result["elevation_smoothed"][1:]))
    moving = sum(1 for p in result["propulsive"] if p) / len(result["propulsive"])

    tiles = "".join(
        f'<div class="tile"><div class="label">{label}</div>'
        f'<div class="value{hero}">{value}</div><div class="note">{note}</div></div>'
        for label, value, note, hero in (
            ("Fade over the ride", f"{change:+.1f} km/h",
             f"{percent:+.1f}% flat-equivalent, fitted start to finish", " hero"),
            ("Per 10 km", f"{signal['per_10km_kmh']:+.2f} km/h", "slope of the fitted trend", ""),
            ("First quarter", f"{signal['quarters'][0]['flat_kmh']:.1f} km/h",
             f"actual {signal['quarters'][0]['actual_kmh']:.1f} km/h", ""),
            ("Last quarter", f"{signal['quarters'][-1]['flat_kmh']:.1f} km/h",
             f"actual {signal['quarters'][-1]['actual_kmh']:.1f} km/h", ""),
            ("Distance", f"{x_max:.1f} km", f"{climb:.0f} m climbed", ""),
            ("Pedalling", f"{100 * moving:.0f}%",
             "of samples propulsive; the rest is coasting or stopped", ""),
        )
    )

    rows = "".join(
        f"<tr><td>{q['from_km']:.1f}&ndash;{q['to_km']:.1f} km</td>"
        f"<td>{q['actual_kmh']:.1f}</td><td>{q['flat_kmh']:.1f}</td></tr>"
        for q in signal["quarters"]
    )
    table = (
        "<details><summary>The numbers behind the fade</summary>"
        "<table><thead><tr><th>Quarter</th><th>Actual km/h</th>"
        "<th>Flat-equivalent km/h</th></tr></thead><tbody>"
        + rows
        + "</tbody></table></details>"
    )

    p = result["params"]
    foot = (
        "Gradient-adjusted speed solves for the power implied by the observed speed and "
        f"gradient, then asks what that power would give on the flat. It assumes "
        f"<code>{p['mass']:g} kg</code> total, <code>CdA {p['cda']:g}</code>, "
        f"<code>Crr {p['crr']:g}</code>, no wind and no drafting. The absolute numbers move "
        "with those guesses; the trend across the ride is much less sensitive to them. "
        "Coasting and stopped samples are excluded, so a descent-heavy finish is not "
        "read as fatigue &mdash; but wind, traffic and deliberate pacing still are."
    )

    charts.page(
        args.out,
        title=args.title or os.path.splitext(os.path.basename(args.gpx))[0],
        subtitle=f"{x_max:.1f} km, {climb:.0f} m climbed, "
                 f"{signal['quarters'][0]['actual_kmh']:.1f} km/h in the first quarter "
                 f"to {signal['quarters'][-1]['actual_kmh']:.1f} in the last",
        tiles=f'<div class="tiles">{tiles}</div>',
        charts="".join(blocks),
        table=table,
        foot=foot,
        data={"km": [round(v, 3) for v in km], "ele": [round(v, 1) for v in ele],
              "speed": [round(v, 2) for v in speed], "flat": [round(v, 2) for v in flat],
              "grade": [round(v, 2) for v in grade], "elapsed": [round(v) for v in elapsed],
              "geom": geom},
        geometry=geometry,
    )

    print(f"{x_max:.1f} km, {climb:.0f} m climbed")
    for q in signal["quarters"]:
        print(f"  {q['from_km']:5.1f}-{q['to_km']:5.1f} km   actual {q['actual_kmh']:5.1f}"
              f"   flat-equivalent {q['flat_kmh']:5.1f} km/h")
    print(f"\nFitted fade {change:+.1f} km/h ({percent:+.1f}%), "
          f"{signal['per_10km_kmh']:+.2f} km/h per 10 km")
    print(f"Wrote {os.path.abspath(args.out)}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="gpx", description="Tools for GPX tracks.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("strava", help="download matching Strava activities as GPX")
    p.add_argument("-k", "--keywords", type=_split, help="comma separated name keywords")
    p.add_argument("-t", "--types", type=_split, help="comma separated sport types, e.g. Ride,Run")
    p.add_argument("-o", "--out", default=DEFAULT_OUT, help=f"output folder (default: {DEFAULT_OUT})")
    p.add_argument("--after", help="only activities on or after YYYY-MM-DD")
    p.add_argument("--before", help="only activities before YYYY-MM-DD")
    p.add_argument("-y", "--yes", action="store_true", help="skip prompts and confirmation")
    p.add_argument("--reauth", action="store_true", help="redo the Strava browser authorisation")
    p.set_defaults(func=cmd_strava)

    c = sub.add_parser("coverage", help="colour a route by whether it was ridden in training")
    c.add_argument("main", help="GPX file of the main route")
    c.add_argument("training", nargs="+", help="GPX files or a quoted glob of training rides")
    c.add_argument("-o", "--out", default="coverage.html", help="output HTML (default: coverage.html)")
    c.add_argument("--tolerance", type=float, default=25.0, help="metres of GPS slack (default: 25)")
    c.add_argument("--angle", type=float, default=45.0,
                   help="degrees a training pass may deviate and still count as the same direction (default: 45)")
    c.add_argument("--resolution", type=float, default=15.0,
                   help="metres per coloured segment (default: 15)")
    c.add_argument("--max-gap", type=float, default=200.0,
                   help="ignore training jumps longer than this many metres (default: 200)")
    c.add_argument("--no-training-layer", action="store_true", help="omit training tracks from the map")
    c.add_argument("--title", help="page title (default: the main file name)")
    c.set_defaults(func=cmd_coverage)

    f = sub.add_parser("profile", help="elevation, speed and gradient-adjusted speed for one ride")
    f.add_argument("gpx", help="GPX file of the ride")
    f.add_argument("-o", "--out", default="profile.html", help="output HTML (default: profile.html)")
    f.add_argument("--mass", type=float, help="rider plus bike, kg (default: 85)")
    f.add_argument("--cda", type=float, help="drag area, m^2 (default: 0.32)")
    f.add_argument("--crr", type=float, help="rolling resistance (default: 0.005)")
    f.add_argument("--smoothing", type=float, default=100.0,
                   help="metres to smooth elevation over before taking gradient (default: 100)")
    f.add_argument("--window", type=float, default=200.0,
                   help="metres over which speed and gradient are both measured (default: 200)")
    f.add_argument("--points", type=int, default=1500, help="samples plotted (default: 1500)")
    f.add_argument("--title", help="page title (default: the file name)")
    f.set_defaults(func=cmd_profile)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except rideprofile.NotEnoughData as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except strava.StravaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
