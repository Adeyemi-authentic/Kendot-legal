"""Consultation bookings: slot -> payment -> conflict check -> calendar invite.

Through the real FastAPI app with a throwaway SQLite store and fakes for Google
Calendar, Paystack and WhatsApp, which record every call. Offline and free.

    ../.venv/bin/python -m pytest -q tests/test_bookings.py        (from api/)
"""

import hashlib
import hmac
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

API = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API / "rag"))
sys.path.insert(0, str(API / "app"))
os.environ.setdefault("API_KEY", "test-admin-key")

import bookings as bk                                   # noqa: E402

ADMIN = {"X-API-Key": os.environ["API_KEY"]}


class FakeCalendar:
    def __init__(self):
        self.calls, self.events, self.busy_blocks = [], {}, []

    def busy(self, start, end):
        self.calls.append(("busy",))
        return list(self.busy_blocks)

    def hold(self, b, note):
        event_id = f"ev{len(self.events) + 1}"
        self.events[event_id] = {"note": note, "attendees": []}
        self.calls.append(("hold", b["id"], note))
        return event_id

    def retitle(self, event_id, b, note):
        self.events[event_id]["note"] = note
        self.calls.append(("retitle", event_id, note))

    def confirm(self, event_id, b, address):
        self.events[event_id].update(attendees=[b["email"]], address=address)
        self.calls.append(("confirm", event_id, b["mode"]))
        return "https://meet.google.com/abc-defg-hij" if b["mode"] == "virtual" else None

    def delete(self, event_id, notify):
        self.events.pop(event_id, None)
        self.calls.append(("delete", event_id, notify))


class FakePaystack:
    secret = "sk_test_fake"

    def __init__(self):
        self.paid = set()            # payment refs Paystack would report as successful
        self.started = []

    def start(self, *, email, amount_kobo, booking_ref, callback_url, metadata):
        pay_ref = f"{booking_ref}-{len(self.started)}"
        self.started.append({"ref": pay_ref, "amount": amount_kobo, "callback": callback_url,
                             "metadata": metadata})
        return pay_ref, f"https://checkout.paystack.test/{pay_ref}"

    def verify(self, pay_ref):
        ok = pay_ref in self.paid
        return {"status": "success" if ok else "abandoned", "currency": "NGN", "amount": 5_000_000}

    def signed(self, raw, signature):
        from payments import Paystack
        return Paystack(self.secret).signed(raw, signature)


class FakeAlerts:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


@pytest.fixture()
def api(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import app as app_module
    monkeypatch.setattr(bk, "SQLITE_PATH", tmp_path / "bookings.sqlite")
    monkeypatch.setenv("ENGINE", "qdrant")
    app_module._hits.clear()
    app_module._busy_cache["at"] = 0.0
    state = app_module.app.state
    state.bookings = bk.BookingStore()
    state.calendar, state.paystack, state.alerts = FakeCalendar(), FakePaystack(), FakeAlerts()
    # No `with`: the lifespan (real stores, real clients) does not run.
    return TestClient(app_module.app), app_module, state


def form(client, **over):
    day = client.get("/booking/slots").json()["days"][0]
    body = {"name": "Ada Obi", "email": "ada@example.com", "phone": "+234 801 234 5678",
            "matter_type": "other", "description": "My landlord wants me out next week.",
            "mode": "virtual", "start": day["slots"][0]["start"], "consent": True}
    return {**body, **over}


def book(client, **over):
    resp = client.post("/bookings", json=form(client, **over))
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- Slots ---------------------------------------------------------------
def test_slots_respect_office_hours_notice_and_weekdays():
    sched = bk.Schedule({})
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)          # Thursday 13:00 WAT
    slots = sched.candidates(now)
    assert slots and all(s >= now + timedelta(hours=24) for s in slots)
    for s in slots:
        local = s.astimezone(bk.WAT)
        assert local.weekday() < 5
        assert (8, 30) <= (local.hour, local.minute) <= (16, 30)     # last start ends 17:15
    first = slots[0].astimezone(bk.WAT)
    assert (first.day, first.hour, first.minute) == (9, 13, 30)       # Friday, 24h later


def test_calendar_busy_blocks_overlapping_slots():
    sched = bk.Schedule({})
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    first = sched.candidates(now)[0]
    busy = [(first + timedelta(minutes=30), first + timedelta(minutes=90))]
    free = sched.free(now, busy, set())
    assert first not in free
    assert first + timedelta(hours=1) not in free                    # overlaps the busy tail
    assert first + timedelta(hours=2) in free


# --- Booking --------------------------------------------------------------
def test_booking_holds_the_slot_and_alerts_the_firm(api):
    client, _, state = api
    b = book(client)
    assert b["status"] == "pending_payment" and b["reference"].startswith("BK-")
    assert "subject to confirmation" in b["message"]
    assert b["whatsapp_url"].startswith("https://wa.me/") and b["reference"] in b["whatsapp_url"]
    assert "description" not in b                                    # never echoed to the browser
    assert [c[0] for c in state.calendar.calls if c[0] != "busy"] == ["hold"]
    assert state.alerts.sent and b["reference"] in state.alerts.sent[0]
    taken = client.get("/booking/slots").json()["days"][0]["slots"]
    assert b["start"] not in [s["start"] for s in taken]


def test_the_same_slot_cannot_be_booked_twice(api):
    client, _, _ = api
    body = form(client)
    assert client.post("/bookings", json=body).status_code == 200
    second = client.post("/bookings", json={**body, "email": "someone@example.com"})
    assert second.status_code == 409


def test_store_index_stops_a_race_for_one_slot(api):
    _, app_module, state = api
    start = datetime(2030, 1, 7, 9, 0, tzinfo=timezone.utc)
    args = dict(start=start, length_min=45, hold=timedelta(minutes=30), amount_kobo=1,
                mode="virtual", office=None, name="A", email="a@x.io", phone="0801",
                matter_type="other", description="x" * 10, page_url=None, consent_text="ok")
    state.bookings.add(**args)
    with pytest.raises(bk.SlotTaken):
        state.bookings.add(**args)


def test_form_needs_email_phone_mode_and_an_office_for_in_person(api):
    client, _, _ = api
    for missing in ("email", "phone", "mode"):
        body = form(client)
        body.pop(missing)
        assert client.post("/bookings", json=body).status_code == 422, missing
    resp = client.post("/bookings", json=form(client, mode="in_person"))
    assert resp.status_code == 422 and "office" in resp.json()["error"]
    ok = book(client, mode="in_person", office="Lagos")
    assert ok["address"] and "Lagos" in ok["address"]


def test_a_time_off_the_list_is_refused(api):
    client, _, _ = api
    resp = client.post("/bookings", json=form(client, start="2030-01-05T03:17:00Z"))
    assert resp.status_code == 409


# --- Payment --------------------------------------------------------------
def test_paystack_flow_then_confirm_sends_invite_with_meet_link(api):
    client, _, state = api
    b = book(client)
    pay = client.post(f"/bookings/{b['token']}/pay").json()
    assert pay["authorization_url"].startswith("https://checkout.paystack.test/")
    started = state.paystack.started[-1]
    assert started["amount"] == 5_000_000 and b["token"] in started["callback"]

    # Back from checkout before paying: still pending.
    assert client.get(f"/bookings/{b['token']}").json()["status"] == "pending_payment"
    state.paystack.paid.add(started["ref"])
    paid = client.get(f"/bookings/{b['token']}").json()
    assert paid["status"] == "paid" and "conflict check" in paid["message"]
    assert any("Paid" in a for a in state.alerts.sent)
    event_id = next(c[1] for c in state.calendar.calls if c[0] == "retitle")
    assert state.calendar.events[event_id]["attendees"] == []        # no invite before confirm

    booking_id = int(b["reference"].split("-")[1])
    done = client.post(f"/admin/bookings/{booking_id}/confirm", headers=ADMIN).json()
    assert done["status"] == "confirmed" and done["meet_url"].startswith("https://meet.google.com/")
    assert state.calendar.events[event_id]["attendees"] == ["ada@example.com"]
    view = client.get(f"/bookings/{b['token']}").json()
    assert view["status"] == "confirmed" and view["meet_url"]


def test_in_person_confirmation_has_the_office_address_and_no_meet_link(api):
    client, _, state = api
    b = book(client, mode="in_person", office="Abuja")
    booking_id = int(b["reference"].split("-")[1])
    client.post(f"/admin/bookings/{booking_id}/mark-paid", headers=ADMIN)
    done = client.post(f"/admin/bookings/{booking_id}/confirm", headers=ADMIN).json()
    assert done["meet_url"] is None
    event = next(iter(state.calendar.events.values()))
    assert "Abuja" in event["address"]


def test_cannot_confirm_before_payment(api):
    client, _, _ = api
    b = book(client)
    booking_id = int(b["reference"].split("-")[1])
    assert client.post(f"/admin/bookings/{booking_id}/confirm", headers=ADMIN).status_code == 409


def test_direct_transfer_extends_the_hold_and_the_firm_marks_it_paid(api):
    client, _, state = api
    b = book(client)
    sent = client.post(f"/bookings/{b['token']}/transfer-sent").json()
    assert sent["status"] == "transfer_pending"
    assert sent["hold_expires_at"] > b["hold_expires_at"]
    assert sent["bank"]["narration"] == b["reference"]
    assert any("Transfer to check" in a for a in state.alerts.sent)
    booking_id = int(b["reference"].split("-")[1])
    paid = client.post(f"/admin/bookings/{booking_id}/mark-paid", headers=ADMIN).json()
    assert paid["status"] == "paid" and paid["payment_method"] == "transfer"


def test_webhook_needs_paystack_signature_and_marks_paid_once(api):
    client, _, state = api
    b = book(client)
    booking_id = int(b["reference"].split("-")[1])
    body = json.dumps({"event": "charge.success", "data": {
        "status": "success", "currency": "NGN", "amount": 5_000_000,
        "reference": f"{b['reference']}-x", "metadata": {"booking_id": booking_id}}}).encode()
    good = hmac.new(FakePaystack.secret.encode(), body, hashlib.sha512).hexdigest()
    headers = {"Content-Type": "application/json"}
    bad = client.post("/payments/paystack/webhook", content=body,
                      headers={**headers, "x-paystack-signature": "nope"})
    assert bad.status_code == 401
    for _ in range(2):                                    # Paystack retries; record it once
        ok = client.post("/payments/paystack/webhook", content=body,
                         headers={**headers, "x-paystack-signature": good})
        assert ok.status_code == 200
    assert client.get(f"/bookings/{b['token']}").json()["status"] == "paid"
    assert sum("Paid" in a for a in state.alerts.sent) == 1


def test_underpayment_does_not_count(api):
    client, _, state = api
    b = book(client)
    booking_id = int(b["reference"].split("-")[1])
    body = json.dumps({"event": "charge.success", "data": {
        "status": "success", "currency": "NGN", "amount": 100,
        "reference": "r", "metadata": {"booking_id": booking_id}}}).encode()
    sig = hmac.new(FakePaystack.secret.encode(), body, hashlib.sha512).hexdigest()
    client.post("/payments/paystack/webhook", content=body, headers={"x-paystack-signature": sig})
    assert client.get(f"/bookings/{b['token']}").json()["status"] == "pending_payment"


# --- Expiry and late payment ----------------------------------------------
def _expire(state, b):
    booking_id = int(b["reference"].split("-")[1])
    state.bookings.set(booking_id, hold_expires_at="2000-01-01T00:00:00Z")
    return booking_id


def test_unpaid_hold_expires_and_frees_the_slot(api):
    client, _, state = api
    b = book(client)
    _expire(state, b)
    view = client.get(f"/bookings/{b['token']}").json()
    assert view["status"] == "expired" and not view["can_pay"]
    assert ("delete", "ev1", False) in state.calendar.calls
    starts = [s["start"] for d in client.get("/booking/slots").json()["days"] for s in d["slots"]]
    assert b["start"] in starts


def test_late_payment_revives_a_free_slot_or_flags_a_taken_one(api):
    client, _, state = api
    b = book(client)
    client.post(f"/bookings/{b['token']}/pay")
    state.paystack.paid.add(state.paystack.started[-1]["ref"])
    _expire(state, b)
    client.get("/booking/slots")                                      # sweep runs
    assert client.get(f"/bookings/{b['token']}").json()["status"] == "paid"

    late = book(client, start=client.get("/booking/slots").json()["days"][0]["slots"][0]["start"])
    client.post(f"/bookings/{late['token']}/pay")
    state.paystack.paid.add(state.paystack.started[-1]["ref"])
    _expire(state, late)
    client.get("/booking/slots")
    book(client, start=late["start"], email="other@example.com")      # someone takes it
    view = client.get(f"/bookings/{late['token']}").json()
    assert view["status"] == "paid_slot_taken"
    assert any("slot taken" in a for a in state.alerts.sent)


# --- Cancel and admin ------------------------------------------------------
def test_cancel_for_conflict_frees_the_slot_and_notifies_only_if_invited(api):
    client, _, state = api
    b = book(client)
    booking_id = int(b["reference"].split("-")[1])
    client.post(f"/admin/bookings/{booking_id}/mark-paid", headers=ADMIN)
    out = client.post(f"/admin/bookings/{booking_id}/cancel", headers=ADMIN,
                      json={"reason": "conflict of interest"}).json()
    assert out["status"] == "cancelled" and out["cancel_reason"] == "conflict of interest"
    assert out["contact"]["whatsapp"].startswith("https://wa.me/234")
    assert ("delete", "ev1", False) in state.calendar.calls          # no invite was ever sent

    c = book(client, email="c@example.com")
    cid = int(c["reference"].split("-")[1])
    client.post(f"/admin/bookings/{cid}/mark-paid", headers=ADMIN)
    client.post(f"/admin/bookings/{cid}/confirm", headers=ADMIN)
    client.post(f"/admin/bookings/{cid}/cancel", headers=ADMIN, json={})
    assert state.calendar.calls[-1][0] == "delete" and state.calendar.calls[-1][2] is True


def test_admin_routes_need_the_key_and_visitor_routes_need_the_token(api):
    client, _, _ = api
    b = book(client)
    booking_id = int(b["reference"].split("-")[1])
    assert client.get("/admin/bookings").status_code == 401
    assert client.post(f"/admin/bookings/{booking_id}/confirm").status_code == 401
    assert client.get(f"/bookings/{b['reference']}").status_code == 404
    listing = client.get("/admin/bookings", headers=ADMIN).json()["bookings"]
    assert listing[0]["description"]                                  # the firm sees it
    assert client.delete(f"/admin/bookings/{booking_id}", headers=ADMIN).status_code == 200
    assert client.get(f"/bookings/{b['token']}").status_code == 404


def test_without_paystack_only_transfer_is_offered(api):
    client, _, state = api
    state.paystack = None
    assert client.get("/booking/options").json()["paystack"] is False
    b = book(client)
    assert b["paystack"] is False
    assert client.post(f"/bookings/{b['token']}/pay").status_code == 404
