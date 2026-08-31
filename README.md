# geepeex

Tools for working with GPX tracks. Each tool is a subcommand of `gpx`.

    gpx strava    download matching Strava activities as GPX into ./gpx

## Setup

    uv sync --extra dev

## `gpx strava`

Strava has **no GPX endpoint**. The only route to a track is
`/activities/{id}/streams`, which returns parallel arrays (`latlng`,
`altitude`, `time`); geepeex reassembles those into trackpoints. The GPX is
therefore built by us, not exported by Strava, so it carries position,
elevation and timestamps but none of the sensor channels (heart rate, power,
cadence) unless we add them.

### One-time Strava app registration

1. Go to <https://www.strava.com/settings/api> and create an API application.
2. Set **Authorization Callback Domain** to `localhost`. Without this Strava
   rejects the redirect and the browser never comes back.
3. Run `gpx strava`. It prompts for the Client ID and Client Secret, opens a
   browser for consent, and stores the resulting refresh token in
   `~/.config/geepeex/strava.json` (mode 0600).

The requested scope is `activity:read_all`, so private activities are included.
Access tokens last six hours and are refreshed automatically; use
`gpx strava --reauth` to redo the browser step.

### Use

Interactive — lists the sport types actually present on the account, prompts
for keywords and types, prints the matches, then asks before downloading:

    gpx strava

Non-interactive:

    gpx strava -k commute,ferry -t Ride,VirtualRide -y
    gpx strava --after 2026-01-01 -t Run -o tracks -y

Keywords are case-insensitive substrings matched against the activity **name**
only (the listing endpoint carries no description). Multiple keywords are OR;
keywords and types together are AND. A blank filter matches everything.

Files land in `./gpx` as `YYYY-MM-DD-slug-<id>.gpx`, using the *local* activity
date. Existing files are skipped, so an interrupted run resumes by re-running.

### Rate limits

Strava allows 100 requests per 15 minutes and 1000 per day. Listing costs one
request per 200 activities; each download costs one more. On a 429 the run
stops, reports how many were written, and tells you to re-run after the window
resets — already-downloaded files are skipped on the retry. Activities with no
`latlng` stream (trainer rides, pool swims) are reported and skipped rather
than written as empty tracks.

## Tests

    uv run --extra dev pytest

## `gpx coverage`

Colours a main route by whether you have ridden each part of it in training.

    gpx coverage route.gpx "gpx/*.gpx" -o coverage.html

- **orange** — never ridden
- **blue** — ridden, but only in the opposite direction
- **green** — ridden in the same direction as the route

Green wins wherever both directions were ridden. Training files may be given as
already-expanded shell arguments or as a quoted glob; the main route is dropped
from the training set automatically, since a glob like `gpx/*.gpx` usually
contains it and would otherwise colour everything green.

### Matching

The route is cut into segments of `--resolution` metres (default 15). A segment
counts as ridden if a training track passes within `--tolerance` metres
(default 25) *and* runs within `--angle` degrees of parallel (default 45). The
angle test is what stops every road crossing the route from marking it as
covered. Training jumps longer than `--max-gap` metres (default 200) are treated
as paused recordings and ignored, so the straight line across a pause does not
paint coverage along a road nobody rode.

Distances use a local equirectangular projection in metres; over the span of a
route the error is far below the GPS tolerance being worked to.

The page is Leaflet over OpenStreetMap tiles, with a legend and per-colour
distances and percentages. Every training track is drawn underneath in light
grey, so you can see where you have been that the route does not go, with the
coloured route always on top. Both layers can be toggled; `--no-training-layer`
leaves the grey out entirely.
