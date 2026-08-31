# geepeex — project decisions

Domain-specific decisions for this repo. General conventions live in the global
`~/.claude/CLAUDE.md`; setup and usage live in `README.md`. This file records
*why* things are the way they are, so they don't get "cleaned up" back into the
problems they were solving.

## Shape

A single `gpx` CLI with subcommands (`geepeex/cli.py` dispatches), each
subcommand one operation on GPX tracks. Chosen over an MCP server so the tools
work standalone in a terminal without Claude in the loop. If MCP access is
wanted later, the subcommand bodies are already thin wrappers over library
functions and can be re-exposed without restructuring.

Later subcommands will render tracks onto an open-source map (Leaflet + OSM
tiles) as a generated webpage. That is why GPX parsing belongs in
`geepeex/gpxfile.py` alongside writing, rather than inside `strava.py` — the
map tools read GPX from disk and never touch Strava.

The Python package is `geepeex` and the default output folder is `gpx`. These
are deliberately different names: a package called `gpx` would shadow the
working folder and break imports from the repo root.

## Strava

**Strava has no GPX endpoint.** The only route to a track is
`/activities/{id}/streams` with `keys=latlng,altitude,time&key_by_type=true`,
which returns parallel arrays that we reassemble into trackpoints. Consequences:
the GPX is ours, not Strava's export, and it carries position/elevation/time but
no sensor channels. Adding heart rate or power means adding stream keys and GPX
extensions, not finding a better endpoint.

Access is the OAuth code flow, not the bulk "Export your data" archive. The
archive holds genuine original files but is manual and stale; the API is live
and filterable.

Gotchas that cost time and will again:

- The Strava app's **Authorization Callback Domain must be `localhost`** or the
  redirect is rejected before the browser returns, with no useful error. Both
  the setup prompt and README say so on purpose.
- Scope must be `activity:read_all`. Plain `activity:read` silently omits
  private activities, which looks like missing data rather than missing scope.
- `type` is deprecated in favour of `sport_type`; `strava.sport_of()` reads
  `sport_type` and falls back, because older cached activities carry only the
  old field.
- `start_date_local` is stamped with a trailing `Z` despite being local wall
  clock time. Parsing it naively as UTC is the correct behaviour here — it
  yields the wall clock — but the `Z` is a lie and should not be trusted
  elsewhere.
- `datetime.fromisoformat` rejects `Z` before Python 3.11, hence
  `parse_strava_time`.

## Filtering

The full activity list is fetched up front and filtered locally, rather than
pushing filters into API query parameters. Costs one request per 200 activities,
buys two things: the prompt can show which sport types actually exist on the
account before asking which are wanted, and keyword semantics stay under our
control. Multiple keywords are OR'd with each other and AND'd with the type
filter. Keywords match the activity **name** only — the listing endpoint carries
no description, and fetching one would cost a request per activity.

## Download behaviour

Filenames are `YYYY-MM-DD-slug-<id>.gpx` using the **local** date: date-led so
the folder sorts chronologically, id-tailed so two same-named rides on one day
cannot collide, local so the day matches the one the rider remembers.

Existing files are skipped. This is the resume mechanism — a run killed by the
rate limit is resumed by re-running it, with no state file.

Activities with no `latlng` stream (trainer rides, pool swims) raise `NoGpsData`
and are reported and skipped. Writing a trackless GPX would look like a
successful download, which is the failure mode worth spending an exception on.

Rate limits are 100 requests / 15 min and 1000 / day. On a 429 the run **stops
and reports** rather than sleeping until the window resets: a silent multi-minute
sleep is worse than an honest exit when re-running is free.

## Credentials

Stored at `~/.config/geepeex/strava.json`, mode 0600, holding client id/secret
and the refresh token. Access tokens are refreshed with 60s of slack so one
cannot expire mid-request. Never log or print the secret or either token.

## Testing

The network path is not covered. Tests exercise the pure logic — filtering, GPX
construction, filename derivation — against literal fixtures. When adding a
Strava behaviour, capture a real response shape into a fixture rather than
mocking `requests`; the failures worth catching are shape changes, not call
counts.

## Coverage colouring (`gpx coverage`)

Green means the training pass ran the same way as the route, blue means it only
ever ran the other way, orange means never. Green outranks blue: an out-and-back
training ride covers a road twice, and the direction the route itself uses is
the one worth reporting.

Three rules keep the result honest, and each exists because dropping it produces
a plausible-looking but wrong map:

- **Angle test.** A nearby training track only counts if it is within `--angle`
  of parallel. Without it every junction a training ride passed through marks
  the route as covered.
- **Gap rule.** Training segments longer than `--max-gap` are dropped. A paused
  recording has a straight jump across it that was never ridden, and densifying
  it paints coverage along roads nobody took.
- **Densifying the main route.** Colour granularity is per segment, so a route
  recorded one point per 200 m would otherwise be coloured in 200 m blocks
  whatever the tolerance.

Geometry is done in a local equirectangular projection in metres
(`coverage.Projection`), not in degrees and not with great-circle formulae. Over
a route's span the error is far under the GPS tolerance, and it makes distance
and angle arithmetic ordinary. Matching uses a uniform bucket grid on segment
midpoints; the cell must be at least `tolerance + resolution` wide or the 3x3
neighbourhood scan misses valid matches near a cell edge.

Rendering collapses consecutive same-colour segments into runs before emitting
polylines — one Leaflet layer per segment is tens of thousands of objects and an
unusable page. Adjacent runs share their joining point so the line has no holes.

Every training track is drawn in light grey beneath the coloured route, giving
context for where the rider has been that the route does not go. It is decimated
for display only — nothing is measured from it, and it is there so a wrong
colouring is obvious at a glance.

The two layers go in explicit Leaflet panes (`trainingPane` 410, `routePane`
420) rather than relying on the order they were added in. Toggling the training
layer in the control re-adds it, which would otherwise lift the grey above the
colouring the page exists to show. The z values sit between Leaflet's own
overlayPane (400) and shadowPane (500).
