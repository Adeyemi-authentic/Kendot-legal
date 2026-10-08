"""One-time Google sign-in for the demo calendar (a personal Google account).

Client builds on Google Workspace use a service account instead (see
api/app/gcal.py); this script is only for calendars where that isn't possible.

1. Google Cloud console > APIs & Services: enable the Google Calendar API.
2. OAuth consent screen: External, add your own address as a test user.
   Then click "Publish app": in Testing mode Google expires refresh tokens after 7 days.
3. Credentials > Create credentials > OAuth client ID > Desktop app.
4. Run (from the repo root, with the client ID and secret it shows you):

       api/.venv/bin/python scripts/google_oauth.py CLIENT_ID CLIENT_SECRET

   Sign in with the calendar's account in the browser that opens. The script
   prints the three GOOGLE_OAUTH_* lines for api/.env and Render.
"""

import http.server
import secrets
import sys
import urllib.parse
import webbrowser

import requests

SCOPES = ("https://www.googleapis.com/auth/calendar.events "
          "https://www.googleapis.com/auth/calendar.freebusy")
PORT = 8765


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    client_id, client_secret = sys.argv[1], sys.argv[2]
    redirect = f"http://127.0.0.1:{PORT}/"
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": state})
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")

        def log_message(self, *args):
            pass

    print(f"Opening the Google sign-in page. If it doesn't open, visit:\n\n{url}\n")
    webbrowser.open(url)
    with http.server.HTTPServer(("127.0.0.1", PORT), Handler) as server:
        while "code" not in got and "error" not in got:
            server.handle_request()
    if got.get("error") or got.get("state") != state:
        sys.exit(f"Sign-in failed: {got.get('error', 'state mismatch')}")

    resp = requests.post("https://oauth2.googleapis.com/token", timeout=15, data={
        "code": got["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code"})
    token = resp.json()
    if "refresh_token" not in token:
        sys.exit(f"No refresh token returned: {token}")
    print("Add these to api/.env (and to Render):\n")
    print(f"GOOGLE_OAUTH_CLIENT_ID={client_id}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={token['refresh_token']}")


if __name__ == "__main__":
    main()
