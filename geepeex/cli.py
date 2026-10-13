"""`gpx` command line: subcommands that operate on GPX tracks."""

import argparse
import glob
import itertools
import os
import sys
from collections import Counter

from . import charts, coverage, merge, mapview, predict, rideprofile, strava
from .gpxfile import (NoGpsData, build_gpx, filename_for, merge_gpx, parse_strava_time,
                      read_metadata, read_points, read_samples, read_tracks, slugify)

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


def _ensure_credentials(reauth=False, scope=strava.SCOPE):
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

    # Only checked for the write scope: configs saved before scope was recorded
    # would otherwise force every read-only user back through the browser.
    if scope != strava.SCOPE and not strava.has_scope(config, scope):
        print(f"This needs Strava permission {scope}, which has not been granted yet.")
        reauth = True

    if reauth or not config.get("refresh_token"):
        config.update(strava.authorize(config["client_id"], config["client_secret"], scope=scope))
        config.pop("athlete", None)  # bulky and never read back
        if scope != strava.SCOPE and not strava.has_scope(config, scope):
            strava.save_config(config)
            raise strava.StravaError(f"Strava did not grant {scope}; tick every box on the consent page")

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


def _local_day(activity):
    return parse_strava_time(activity.get("start_date_local") or activity["start_date"]).strftime("%Y-%m-%d")


def cmd_merge(args):
    config = _ensure_credentials(reauth=args.reauth)
    client = strava.StravaClient(config, on_config_change=strava.save_config)

    recent = list(itertools.islice(client.activities(), args.last))
    if not recent:
        print("No activities on the account.")
        return 1
    print(f"\nLast {len(recent)} activities, newest first:\n")
    _print_table(recent)

    picked = [recent[i] for i in merge.parse_selection(
        _prompt("\nActivities to merge (e.g. 2,3 or 2-4): "), len(recent))]
    picked.sort(key=lambda a: a["start_date"])
    archive_dir = os.path.join(args.out, "archive")
    print(f"\nWill archive in full to {os.path.abspath(archive_dir)} and merge, in this order:\n")
    _print_table(picked)
    if not _confirm("\nContinue?"):
        print("Cancelled. Nothing fetched.")
        return 1

    snapshots = []
    for activity in picked:
        path = merge.find_archive(archive_dir, activity["id"])
        if path:
            print(f"  already archived: {path}")
        else:
            try:
                path = merge.save_archive(archive_dir, client.snapshot(activity["id"]))
            except strava.RateLimited as exc:
                print(f"\n{exc}\nArchives written so far are kept; re-run to carry on.")
                return 2
            print(f"  archived: {path}")
        snapshots.append(merge.load_archive(path))

    parts = merge.check_mergeable([(s["activity"], s["streams"]) for s in snapshots])
    first = parts[0][0]
    sports = list(dict.fromkeys(strava.sport_of(a) for a, _ in parts))
    if len(sports) > 1:
        print(f"\nThese are different sports: {', '.join(sports)}.")
    name = args.name or _prompt(f"\nName for the merged activity [{first.get('name')}]: ", first.get("name"))
    sport = args.sport or (sports[0] if len(sports) == 1 else
                           _prompt(f"Sport type, one of {', '.join(sports)} [{sports[0]}]: ", sports[0]))
    # Anything else is only checked by Strava after the upload has gone through.
    if not args.sport and sport not in sports:
        raise merge.CannotMerge(f"{sport!r} is not one of {', '.join(sports)}; use --sport for another")
    desc = "\n\n".join(a["description"] for a, _ in parts if a.get("description"))

    out = os.path.join(args.out, "merged", f"{_local_day(first)}-{slugify(name)}-merged.gpx")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        fh.write(merge_gpx(parts, name, sport, desc))
    ids = ",".join(str(a["id"]) for a, _ in parts)
    print(f"\nWrote {out}")
    print(f"To upload later: gpx upload {out} --replaces {ids}")

    if not _confirm("\nUpload it to Strava now? The originals have to be deleted by hand first."):
        return 0
    return _replace_and_upload(out, [a["id"] for a, _ in parts], archive_dir)


def cmd_upload(args):
    if not os.path.isfile(args.gpx):
        raise strava.StravaError(f"not found: {args.gpx}")
    return _replace_and_upload(args.gpx, args.replaces or [], args.archive, reauth=args.reauth)


def _replace_and_upload(path, replaces, archive_dir, reauth=False):
    """Upload `path` once every activity it replaces is archived locally and
    gone from Strava. Strava rejects a track duplicating one still on the
    account, and its API cannot delete, so the deleting is the rider's."""
    archives = {i: merge.find_archive(archive_dir, i) for i in replaces}
    missing = [str(i) for i, p in archives.items() if not p]
    if missing:
        raise strava.StravaError(
            f"no local archive in {archive_dir} for {', '.join(missing)}; "
            "refusing to have anything deleted that is not kept here first"
        )

    config = _ensure_credentials(reauth=reauth, scope=strava.WRITE_SCOPE)
    client = strava.StravaClient(config, on_config_change=strava.save_config)

    remaining = [i for i in replaces if client.exists(i)]
    if remaining:
        print(
            "\nThese activities are still on Strava and must be deleted before the upload.\n"
            "Deleting them loses kudos, comments, photos, segment efforts and the original\n"
            "device file. The local archive keeps the track, every sensor stream, the\n"
            "activity details, laps, comments and the list of who gave kudos.\n"
        )
        for i in remaining:
            print(f"  https://www.strava.com/activities/{i}\n    archived in {archives[i]}")
        if not _confirm("\nDelete these yourself on Strava, then come back. Go ahead?"):
            print("Stopped. Nothing uploaded.")
            return 1
    while remaining:
        if _prompt("\nPress Enter once they are deleted (q to stop): ").lower() == "q":
            print("Stopped. Nothing uploaded.")
            return 1
        remaining = [i for i in remaining if client.exists(i)]
        for i in remaining:
            print(f"  still on Strava: https://www.strava.com/activities/{i}")

    meta = read_metadata(path)
    print(f"\nUploading {path}...")
    activity_id = client.wait_for_upload(client.upload(path, meta.get("name"), meta.get("desc")))
    # Not a documented upload field; set afterwards so the merged activity
    # keeps the originals' sport rather than whatever Strava guesses.
    print(f"Uploaded: https://www.strava.com/activities/{activity_id}")
    if meta.get("type"):
        try:
            client.set_sport(activity_id, meta["type"])
        except strava.StravaError as exc:
            print(f"warning: could not set sport to {meta['type']!r}, set it on Strava: {exc}")
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


def _hhmm(seconds):
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}h {rest // 60:02d}m" if hours else f"{rest // 60}m"


def cmd_predict(args):
    if not os.path.isfile(args.route):
        raise strava.StravaError(f"route not found: {args.route}")
    route = [p for seg in read_samples(args.route) for p in seg]
    if len(route) < 2:
        raise strava.StravaError(f"{args.route} has no track points")
    if any(p[2] is None for p in route):
        raise strava.StravaError(f"{args.route} has no elevation; gradient cannot be derived")

    training_files = _expand(args.training, exclude=[args.route])
    if not training_files:
        raise strava.StravaError("no training files matched")

    physics = {k: v for k, v in (("mass", args.mass), ("cda", args.cda), ("crr", args.crr))
               if v is not None}
    params = dict(rideprofile.DEFAULTS, **physics)

    rides, skipped = [], []
    for path in training_files:
        try:
            result = rideprofile.analyse(read_samples(path), window=args.window, **physics)
            fit = predict.ride_fitness(result)
        except (rideprofile.NotEnoughData, predict.NotEnoughRides) as exc:
            skipped.append((path, str(exc)))
            continue
        if fit is None:
            skipped.append((path, "too few propulsive samples"))
        elif fit["hours"] * 60 < args.min_minutes:
            skipped.append((path, f"shorter than {args.min_minutes:g} minutes"))
        else:
            fit["path"] = path
            fit["date"] = _ride_date(path)
            rides.append(fit)

    for path, why in skipped:
        print(f"  skipped {os.path.basename(path)}: {why}")
    if not rides:
        raise predict.NotEnoughRides("no training ride was usable")

    fit = predict.fitness(rides, half_life_days=args.half_life)
    forecast = predict.predict(
        [(p[0], p[1]) for p in route], [p[2] for p in route], fit, params,
        window=args.window, smoothing=args.smoothing,
    )

    n = args.points
    km = [d / 1000 for d in _stride(forecast["distance"], n)]
    x_max = km[-1]
    ele = _stride(forecast["elevation_smoothed"], n)
    speed = [v * 3.6 for v in _stride(forecast["speed"], n)]
    flat = [v * 3.6 for v in _stride(forecast["flat_speed"], n)]
    grade = [g * 100 for g in _stride(forecast["grade"], n)]
    elapsed = _stride(forecast["elapsed"], n)

    geom, blocks = {}, []
    for chart_id, title, unit, series, fill in (
        ("elevation", "Elevation", "metres", [("Elevation", ele, 0)], True),
        ("speed", "Predicted speed", "km/h on this route", [("Predicted", speed, 0)], False),
        ("flat", "Predicted gradient-adjusted speed",
         "km/h on the flat: the modelled fade, with the road taken out",
         [("Flat-equivalent", flat, 0)], False),
    ):
        html, part = charts.line_chart(chart_id, title, unit, km, series, x_max, fill=fill)
        blocks.append(html)
        geom.update(part)

    seconds = forecast["seconds"]
    climb = sum(max(0.0, b - a) for a, b in zip(forecast["elevation_smoothed"],
                                                forecast["elevation_smoothed"][1:]))
    hours = seconds / 3600
    beyond = hours > fit["longest_hours"]

    tiles = "".join(
        f'<div class="tile"><div class="label">{label}</div>'
        f'<div class="value{hero}">{value}</div><div class="note">{note}</div></div>'
        for label, value, note, hero in (
            ("Predicted moving time", _hhmm(seconds),
             "no stops, no wind, no traffic", " hero"),
            ("Average speed", f"{x_max / hours:.1f} km/h", f"over {x_max:.1f} km", ""),
            ("Climbing", f"{climb:.0f} m", "on the route as recorded", ""),
            ("Modelled power", f"{fit['start_watts']:.0f} to "
                               f"{forecast['power'][-1]:.0f} W",
             "estimate, and only as good as the assumed mass and drag", ""),
            ("Fitness from", f"{len(rides)} rides",
             f"half-life {fit['half_life_days']:g} days; newest "
             f"{fit['rides'][0]['date'].date()}", ""),
            ("Longest training ride", _hhmm(fit["longest_hours"] * 3600),
             "beyond this the fade is extrapolated" if beyond
             else "the prediction stays inside this", ""),
        )
    )

    rows = "".join(
        f"<tr><td>{r['date'].date()}</td><td>{r['km']:.1f}</td>"
        f"<td>{_hhmm(r['hours'] * 3600)}</td><td>{r['mean_watts']:.0f}</td>"
        f"<td>{r['decay_watts_per_hour']:+.0f}</td>"
        f"<td>{100 * r['weight'] / sum(x['weight'] for x in rides):.0f}%</td></tr>"
        for r in fit["rides"]
    )
    table = ("<details><summary>The rides this prediction is built from</summary>"
             "<table><thead><tr><th>Date</th><th>km</th><th>Moving</th>"
             "<th>Mean W (est)</th><th>W/hour (est)</th><th>Weight</th></tr></thead>"
             f"<tbody>{rows}</tbody></table></details>")

    warnings = []
    if beyond:
        warnings.append(f"This route is predicted to take {_hhmm(seconds)}, longer than the "
                        f"longest ride it learned from ({_hhmm(fit['longest_hours'] * 3600)}). "
                        "The fade past that point is extrapolation, not evidence.")
    if forecast["floored"]:
        warnings.append("The modelled power hit its floor before the finish, so the last part "
                        "of the route is predicted at that floor rather than at a fitted value.")
    if forecast["crawled"]:
        warnings.append("Somewhere on this route the model predicts under 1 m/s and was clamped "
                        "there; a gradient that steep is probably being walked.")

    foot = (
        "Every ride is reduced to the power it started with and how fast that decayed, then "
        f"averaged with recent rides counting for more (half-life {fit['half_life_days']:g} days, "
        "weighted also by ride duration since a short ride cannot show a fade). That power is "
        "played forward over this route's gradients. It is a prediction of <em>moving</em> time "
        "with no wind, no drafting, no stops and no traffic, from a model whose mass and drag "
        f"(<code>{params['mass']:g} kg</code>, <code>CdA {params['cda']:g}</code>) are guesses. "
        "Treat it as a comparison between routes, not a time to aim at."
        + "".join(f"<br><br><strong>{w}</strong>" for w in warnings)
    )

    charts.page(
        args.out,
        title=args.title or os.path.splitext(os.path.basename(args.route))[0] + " forecast",
        subtitle=f"{x_max:.1f} km, {climb:.0f} m climbed, predicted {_hhmm(seconds)} moving "
                 f"at {x_max / hours:.1f} km/h, from {len(rides)} recent rides",
        tiles=f'<div class="tiles">{tiles}</div>',
        charts="".join(blocks),
        table=table,
        foot=foot,
        data={"km": [round(v, 3) for v in km], "ele": [round(v, 1) for v in ele],
              "speed": [round(v, 2) for v in speed], "flat": [round(v, 2) for v in flat],
              "grade": [round(v, 2) for v in grade], "elapsed": [round(v) for v in elapsed],
              "geom": geom},
        geometry={"w": charts.W, "left": charts.LEFT, "right": charts.RIGHT},
    )

    print(f"\n{x_max:.1f} km, {climb:.0f} m climbed")
    print(f"From {len(rides)} rides, newest {fit['rides'][0]['date'].date()}: "
          f"{fit['start_watts']:.0f} W start, {fit['decay_watts_per_hour']:+.1f} W/hour (est)")
    print(f"Predicted moving time {_hhmm(seconds)} at {x_max / hours:.1f} km/h")
    for w in warnings:
        print(f"  ! {w}")
    print(f"Wrote {os.path.abspath(args.out)}")
    return 0


def _ride_date(path):
    for segment in read_samples(path):
        for _, _, _, time in segment:
            if time is not None:
                return time
    raise rideprofile.NotEnoughData(f"{path} has no timestamps")


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

    m = sub.add_parser("merge", help="merge recent Strava activities into one GPX, optionally uploading it")
    m.add_argument("-n", "--last", type=int, default=20, help="how many recent activities to list (default: 20)")
    m.add_argument("-o", "--out", default=DEFAULT_OUT,
                   help=f"folder for archive/ and merged/ (default: {DEFAULT_OUT})")
    m.add_argument("--name", help="name of the merged activity (default: prompt)")
    m.add_argument("--sport", help="sport type of the merged activity (default: the originals')")
    m.add_argument("--reauth", action="store_true", help="redo the Strava browser authorisation")
    m.set_defaults(func=cmd_merge)

    u = sub.add_parser("upload", help="upload a GPX to Strava, after the activities it replaces are deleted")
    u.add_argument("gpx", help="GPX file to upload")
    u.add_argument("--replaces", type=lambda v: [int(i) for i in _split(v)], help="comma separated ids that must be archived and deleted first")
    u.add_argument("--archive", default=os.path.join(DEFAULT_OUT, "archive"),
                   help=f"where the archives of replaced activities are (default: {DEFAULT_OUT}/archive)")
    u.add_argument("--reauth", action="store_true", help="redo the Strava browser authorisation")
    u.set_defaults(func=cmd_upload)

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

    r = sub.add_parser("predict", help="predict how long a route will take, from recent rides")
    r.add_argument("route", help="GPX file of the route to predict")
    r.add_argument("training", nargs="+", help="GPX files or a quoted glob of recent rides")
    r.add_argument("-o", "--out", default="predict.html", help="output HTML (default: predict.html)")
    r.add_argument("--half-life", type=float, default=predict.DEFAULT_HALF_LIFE,
                   help="days after which a ride counts half as much (default: 42)")
    r.add_argument("--min-minutes", type=float, default=20.0,
                   help="ignore training rides shorter than this (default: 20)")
    r.add_argument("--mass", type=float, help="rider plus bike, kg (default: 85)")
    r.add_argument("--cda", type=float, help="drag area, m^2 (default: 0.32)")
    r.add_argument("--crr", type=float, help="rolling resistance (default: 0.005)")
    r.add_argument("--window", type=float, default=200.0,
                   help="metres over which speed and gradient are measured (default: 200)")
    r.add_argument("--smoothing", type=float, default=100.0,
                   help="metres to smooth the displayed elevation over (default: 100)")
    r.add_argument("--points", type=int, default=1500, help="samples plotted (default: 1500)")
    r.add_argument("--title", help="page title (default: the route file name)")
    r.set_defaults(func=cmd_predict)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (rideprofile.NotEnoughData, predict.NotEnoughRides) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (strava.StravaError, merge.CannotMerge) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
