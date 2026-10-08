"""Google Calendar for consultation bookings: free/busy, holds and invites.

The firm's calendar is the source of truth for when a lawyer is free. A booking
puts a private HOLD on it straight away (no guests, so nothing is sent). When the
firm confirms, the hold becomes the real event: the visitor is added as a guest,
virtual consultations get a Google Meet link, and Google emails the invite.

Two ways to sign in, chosen by which env vars are set:

  Google Workspace (client builds)  GOOGLE_SERVICE_ACCOUNT_JSON + GOOGLE_CALENDAR_IMPERSONATE
      A service account with domain-wide delegation, acting as the firm's user.
      Without delegation Google refuses to invite guests or create Meet links.
  Personal Google account (demo)    GOOGLE_OAUTH_CLIENT_ID + _CLIENT_SECRET + _REFRESH_TOKEN
      A one-time sign-in (scripts/google_oauth.py) that yields a refresh token.

Neither set: NoCalendar, so bookings still work in development (every slot in
office hours is free and nothing is written anywhere).

What goes on the calendar is deliberately thin: reference, matter type, mode.
The visitor's description of their problem stays in the database, because
calendars get shared, synced to phones and shown on lock screens.
"""

import json
import os
import pathlib
import sys
from datetime import datetime

from bookings import WAT, parse, reference

API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.freebusy"]


class CalendarError(Exception):
    pass


def _credentials(env):
    if env.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        from google.oauth2 import service_account
        raw = env["GOOGLE_SERVICE_ACCOUNT_JSON"].strip()
        info = json.loads(raw if raw.startswith("{") else pathlib.Path(raw).read_text())
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        subject = env.get("GOOGLE_CALENDAR_IMPERSONATE")
        return creds.with_subject(subject) if subject else creds
    if env.get("GOOGLE_OAUTH_REFRESH_TOKEN"):
        from google.oauth2.credentials import Credentials
        return Credentials(
            None, refresh_token=env["GOOGLE_OAUTH_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=env["GOOGLE_OAUTH_CLIENT_ID"],
            client_secret=env["GOOGLE_OAUTH_CLIENT_SECRET"], scopes=SCOPES)
    return None


class GoogleCalendar:
    def __init__(self, creds, calendar_id: str, firm: str):
        from google.auth.transport.requests import AuthorizedSession
        self.http = AuthorizedSession(creds)
        self.calendar_id = calendar_id
        self.firm = firm

    def _call(self, method, path, **kw):
        resp = self.http.request(method, f"{API}{path}", timeout=15, **kw)
        if resp.status_code == 410 and method == "DELETE":
            return {}                                   # already gone
        if resp.status_code >= 400:
            raise CalendarError(f"{method} {path}: {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.content else {}

    @property
    def _events(self):
        return f"/calendars/{self.calendar_id}/events"

    def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                "items": [{"id": self.calendar_id}]}
        cal = self._call("POST", "/freeBusy", json=body)["calendars"][self.calendar_id]
        if cal.get("errors"):
            raise CalendarError(f"freeBusy: {cal['errors']}")
        return [(parse(b["start"]), parse(b["end"])) for b in cal.get("busy", [])]

    def hold(self, b: dict, note: str) -> str:
        event = {
            "summary": f"HOLD {reference(b['id'])}: {note}",
            "description": (f"Reference: {reference(b['id'])}\nMatter: {b['matter_type']}\n"
                            f"Mode: {b['mode'].replace('_', ' ')}"
                            + (f" ({b['office']})" if b["office"] else "")
                            + "\n\nNo invite has been sent. Confirm or cancel on the bookings page."),
            "start": {"dateTime": b["start_at"], "timeZone": "Africa/Lagos"},
            "end": {"dateTime": b["end_at"], "timeZone": "Africa/Lagos"},
            "visibility": "private", "transparency": "opaque", "colorId": "5",
        }
        return self._call("POST", self._events, params={"sendUpdates": "none"}, json=event)["id"]

    def retitle(self, event_id: str, b: dict, note: str) -> None:
        self._call("PATCH", f"{self._events}/{event_id}", params={"sendUpdates": "none"},
                   json={"summary": f"HOLD {reference(b['id'])}: {note}"})

    def confirm(self, event_id: str, b: dict, address: str | None) -> str | None:
        """Turn the hold into the consultation and invite the visitor. Returns the Meet link."""
        local = parse(b["start_at"]).astimezone(WAT)
        minutes = int((parse(b["end_at"]) - parse(b["start_at"])).total_seconds() // 60)
        where = ("by video call (Google Meet link in this invite)" if b["mode"] == "virtual"
                 else f"at our {b['office']} office: {address}")
        event = {
            "summary": f"Consultation with {self.firm} ({reference(b['id'])})",
            "description": (f"Your consultation with {self.firm} on {local:%A %d %B at %H:%M} WAT, "
                            f"{where}. It lasts up to {minutes} minutes.\n\nReference: {reference(b['id'])}. "
                            "To rearrange, reply to this invite or message us on WhatsApp."),
            "attendees": [{"email": b["email"], "displayName": b["name"]}],
            "visibility": "private", "colorId": "10",
            "location": address if b["mode"] == "in_person" else None,
        }
        if b["mode"] == "virtual":
            event["conferenceData"] = {"createRequest": {
                "requestId": b["token"], "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        out = self._call("PATCH", f"{self._events}/{event_id}",
                         params={"sendUpdates": "all", "conferenceDataVersion": 1}, json=event)
        return out.get("hangoutLink")

    def delete(self, event_id: str, notify: bool) -> None:
        self._call("DELETE", f"{self._events}/{event_id}",
                   params={"sendUpdates": "all" if notify else "none"})


class NoCalendar:
    """Development stand-in: every office-hours slot is free, nothing is written."""

    def busy(self, start, end):
        return []

    def hold(self, b, note):
        return None

    def retitle(self, event_id, b, note):
        pass

    def confirm(self, event_id, b, address):
        return None

    def delete(self, event_id, notify):
        pass


def make_calendar(firm: str, env=os.environ):
    creds = _credentials(env)
    if creds is None:
        print("[startup] calendar: none (set GOOGLE_* to use Google Calendar)", file=sys.stderr)
        return NoCalendar()
    calendar_id = env.get("GOOGLE_CALENDAR_ID") or env.get("GOOGLE_CALENDAR_IMPERSONATE") or "primary"
    print(f"[startup] calendar: Google ({calendar_id})", file=sys.stderr)
    return GoogleCalendar(creds, calendar_id, firm)

