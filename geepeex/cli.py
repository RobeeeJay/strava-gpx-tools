"""`gpx` command line: subcommands that operate on GPX tracks."""

import argparse
import glob
import os
import sys
from collections import Counter

from . import coverage, mapview, strava
from .gpxfile import NoGpsData, build_gpx, filename_for, parse_strava_time, read_points, read_tracks

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

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except strava.StravaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
