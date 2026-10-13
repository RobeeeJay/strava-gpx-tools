"""Strava OAuth, activity listing and stream fetching.

Strava has no GPX endpoint. The only way to a track is
/activities/{id}/streams, which returns parallel arrays we reassemble into
trackpoints ourselves (see gpxfile.build_gpx).
"""

import http.server
import json
import os
import secrets
import stat
import threading
import time
import urllib.parse
import webbrowser

import requests

API = "https://www.strava.com/api/v3"
OAUTH = "https://www.strava.com/oauth"
# activity:read_all rather than activity:read, or private activities are invisible.
SCOPE = "activity:read_all"
CALLBACK_PORT = 8721
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"

CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "geepeex",
    "strava.json",
)


class StravaError(Exception):
    pass


class RateLimited(StravaError):
    pass


# --- credential storage -----------------------------------------------------


def load_config(path=CONFIG_PATH):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def save_config(config, path=CONFIG_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(config, fh, indent=2)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


# --- one-time browser consent ----------------------------------------------


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result = None

    def do_GET(self):
        # Browsers follow the callback with a /favicon.ico request; recording
        # that would replace the real callback with an empty one before the
        # poll loop reads it, which surfaces as a state mismatch.
        url = urllib.parse.urlparse(self.path)
        if url.path != "/callback" or _CallbackHandler.result is not None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        query = urllib.parse.parse_qs(url.query)
        _CallbackHandler.result = query
        body = b"Strava authorised. Close this tab and return to the terminal."
        if "code" not in query:
            body = b"Authorisation failed or was declined. Return to the terminal."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def authorize(client_id, client_secret, timeout=300):
    """Run the OAuth code flow against a throwaway localhost server.

    The Strava app's "Authorization Callback Domain" must be set to `localhost`
    or Strava refuses the redirect before the browser ever comes back.
    """
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": SCOPE,
        "state": state,
    }
    url = f"{OAUTH}/authorize?" + urllib.parse.urlencode(params)

    _CallbackHandler.result = None
    server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"\nOpening browser for Strava authorisation.\nIf it does not open: {url}\n")
    webbrowser.open(url)

    deadline = time.time() + timeout
    while _CallbackHandler.result is None and time.time() < deadline:
        time.sleep(0.2)
    server.shutdown()

    result = _CallbackHandler.result
    if result is None:
        raise StravaError("timed out waiting for Strava authorisation")
    if result.get("state", [None])[0] != state:
        raise StravaError("state mismatch on OAuth callback")
    if "code" not in result:
        raise StravaError(f"Strava declined authorisation: {result.get('error', ['unknown'])[0]}")

    granted = result.get("scope", [""])[0]
    if "activity:read_all" not in granted:
        print("Warning: activity:read_all was not granted; private activities will be skipped.")

    response = requests.post(
        f"{OAUTH}/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"][0],
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if not response.ok:
        raise StravaError(f"token exchange failed: {response.status_code} {response.text}")
    return response.json()


# --- client -----------------------------------------------------------------


class StravaClient:
    def __init__(self, config, on_config_change=None):
        self.config = config
        self._on_config_change = on_config_change
        self.session = requests.Session()
        self.usage = None  # last seen X-RateLimit-Usage, as "short,daily"

    def _access_token(self):
        # 60s of slack so a token cannot expire mid-request.
        if self.config.get("expires_at", 0) > time.time() + 60:
            return self.config["access_token"]
        response = requests.post(
            f"{OAUTH}/token",
            data={
                "client_id": self.config["client_id"],
                "client_secret": self.config["client_secret"],
                "refresh_token": self.config["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if not response.ok:
            raise StravaError(
                f"token refresh failed: {response.status_code} {response.text}\n"
                "Re-run with --reauth to grant access again."
            )
        self.config.update(response.json())
        if self._on_config_change:
            self._on_config_change(self.config)
        return self.config["access_token"]

    def get(self, path, **params):
        response = self.session.get(
            f"{API}{path}",
            headers={"Authorization": f"Bearer {self._access_token()}"},
            params=params,
            timeout=60,
        )
        self.usage = response.headers.get("X-RateLimit-Usage")
        if response.status_code == 429:
            raise RateLimited(
                f"Strava rate limit hit (usage {self.usage} of "
                f"{response.headers.get('X-RateLimit-Limit')}). "
                "The 15-minute window resets on the quarter hour; re-run then — "
                "already-downloaded files are skipped."
            )
        if response.status_code == 404:
            raise StravaError(f"not found: {path}")
        if not response.ok:
            raise StravaError(f"{path} failed: {response.status_code} {response.text}")
        return response.json()

    def activities(self, after=None, before=None, page_size=200, max_pages=100):
        """Page through /athlete/activities newest-first until Strava runs dry."""
        page = 1
        while page <= max_pages:
            params = {"page": page, "per_page": page_size}
            if after:
                params["after"] = after
            if before:
                params["before"] = before
            batch = self.get("/athlete/activities", **params)
            if not batch:
                return
            yield from batch
            if len(batch) < page_size:
                return
            page += 1

    def streams(self, activity_id):
        return self.get(
            f"/activities/{activity_id}/streams",
            keys="latlng,altitude,time",
            key_by_type="true",
        )


# --- filtering --------------------------------------------------------------


def sport_of(activity):
    return activity.get("sport_type") or activity.get("type") or ""


def matches(activity, keywords=(), types=()):
    """Keyword match is case-insensitive substring against the activity name
    (the listing endpoint carries no description). Empty filter matches all."""
    if types:
        wanted = {t.strip().lower() for t in types if t.strip()}
        if wanted and sport_of(activity).lower() not in wanted:
            return False
    if keywords:
        name = (activity.get("name") or "").lower()
        terms = [k.strip().lower() for k in keywords if k.strip()]
        if terms and not any(term in name for term in terms):
            return False
    return True


def filter_activities(activities, keywords=(), types=()):
    return [a for a in activities if matches(a, keywords, types)]
