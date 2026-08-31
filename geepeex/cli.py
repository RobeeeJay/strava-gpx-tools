"""`gpx` command line: subcommands that operate on GPX tracks."""

import argparse
import os
import sys
from collections import Counter

from . import strava
from .gpxfile import NoGpsData, build_gpx, filename_for, parse_strava_time

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
