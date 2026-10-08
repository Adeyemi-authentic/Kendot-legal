"""Consultation bookings: the store, the slot rules and the status flow.

A visitor picks a slot and the booking is held for them while they pay. The
firm confirms after a conflict check (the Google Calendar invite goes out then),
or cancels.

    pending_payment ──Paystack──▶ paid ──firm──▶ confirmed
          │      └──"I've sent a transfer"──▶ transfer_pending ──firm marks paid──▶ paid
          └──hold runs out──▶ expired                      any active ──firm──▶ cancelled

A late payment for an expired hold revives the booking if the slot is still
free, and otherwise lands in paid_slot_taken for the firm to rebook or refund.

Same storage switch as leads.py: ENGINE=pg uses Postgres (DATABASE_URL), anything
else a local SQLite file. Times are stored as UTC ISO strings ("2026-10-14T09:00:00Z")
in both, so they sort and compare the same way everywhere.

A partial unique index on start_at over the active statuses is what stops two
people holding the same slot, even if two requests race.
"""

import os
import pathlib
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
TABLE = os.environ.get("BOOKINGS_TABLE", "kendot_bookings")
SQLITE_PATH = HERE.parent / "rag" / "bookings.sqlite"

WAT = timezone(timedelta(hours=1), "WAT")    # Nigeria: UTC+1 all year, no DST
ACTIVE = ("pending_payment", "transfer_pending", "paid", "confirmed")
MODES = ("virtual", "in_person")

COLUMNS = ("id", "token", "created_at", "updated_at", "status", "start_at", "end_at",
           "mode", "office", "name", "email", "phone", "matter_type", "description",
           "page_url", "consent_text", "consent_at", "hold_expires_at", "amount_kobo",
           "payment_method", "payment_ref", "paid_at", "calendar_event_id", "meet_url",
           "cancel_reason")


class SlotTaken(Exception):
    """Someone else holds that slot."""


# --- Time helpers ------------------------------------------------------------
def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("time must include a timezone")
    return dt.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def reference(booking_id: int) -> str:
    return f"BK-{booking_id:04d}"


def label(start: datetime) -> str:
    """'Tue 14 Oct, 10:00 WAT' for messages and alerts."""
    local = start.astimezone(WAT)
    return f"{local:%a} {local.day} {local:%b}, {local:%H:%M} WAT"


# --- Slot rules --------------------------------------------------------------
class Schedule:
    """When consultations can start. All from env, with the firm's published hours."""

    def __init__(self, env=os.environ):
        self.length = int(env.get("BOOKING_LENGTH_MIN", "45"))
        self.step = int(env.get("BOOKING_STEP_MIN", "60"))           # 45 min + 15 min buffer
        self.day_start = time.fromisoformat(env.get("BOOKING_DAY_START", "08:30"))
        self.day_end = time.fromisoformat(env.get("BOOKING_DAY_END", "17:30"))
        self.days_ahead = int(env.get("BOOKING_DAYS_AHEAD", "14"))
        self.min_notice = timedelta(hours=float(env.get("BOOKING_MIN_NOTICE_HOURS", "24")))
        self.weekdays = {int(d) for d in env.get("BOOKING_WEEKDAYS", "0,1,2,3,4").split(",")}

    def candidates(self, now: datetime) -> list[datetime]:
        """Every start time in the booking window, before anything is busy."""
        earliest = now + self.min_notice
        first_day = now.astimezone(WAT).date()
        out = []
        for i in range(self.days_ahead + 1):
            day: date = first_day + timedelta(days=i)
            if day.weekday() not in self.weekdays:
                continue
            start = datetime.combine(day, self.day_start, WAT)
            close = datetime.combine(day, self.day_end, WAT)
            while start + timedelta(minutes=self.length) <= close:
                if start >= earliest:
                    out.append(start.astimezone(timezone.utc))
                start += timedelta(minutes=self.step)
        return out

    def free(self, now: datetime, busy: list[tuple[datetime, datetime]],
             held: set[str]) -> list[datetime]:
        """Candidates minus calendar busy blocks and slots held in our own table."""
        length = timedelta(minutes=self.length)
        return [s for s in self.candidates(now)
                if iso(s) not in held
                and not any(s < b_end and b_start < s + length for b_start, b_end in busy)]


# --- Store -------------------------------------------------------------------
class BookingStore:
    def __init__(self):
        self.pg = os.environ.get("ENGINE", "qdrant").lower() == "pg"
        self.ph = "%s" if self.pg else "?"
        id_col = "id SERIAL PRIMARY KEY" if self.pg else "id INTEGER PRIMARY KEY AUTOINCREMENT"
        active = ", ".join(f"'{s}'" for s in ACTIVE)
        with self._connect() as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                    {id_col},
                    token             TEXT NOT NULL UNIQUE,
                    created_at        TEXT NOT NULL,
                    updated_at        TEXT NOT NULL,
                    status            TEXT NOT NULL,
                    start_at          TEXT NOT NULL,
                    end_at            TEXT NOT NULL,
                    mode              TEXT NOT NULL,
                    office            TEXT,
                    name              TEXT NOT NULL,
                    email             TEXT NOT NULL,
                    phone             TEXT NOT NULL,
                    matter_type       TEXT NOT NULL,
                    description       TEXT NOT NULL,
                    page_url          TEXT,
                    consent_text      TEXT NOT NULL,
                    consent_at        TEXT NOT NULL,
                    hold_expires_at   TEXT,
                    amount_kobo       INTEGER NOT NULL,
                    payment_method    TEXT,
                    payment_ref       TEXT,
                    paid_at           TEXT,
                    calendar_event_id TEXT,
                    meet_url          TEXT,
                    cancel_reason     TEXT
                )
            """)
            conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {TABLE}_one_per_slot "
                         f"ON {TABLE} (start_at) WHERE status IN ({active})")

    def _connect(self):
        """A short-lived connection per call, like leads.py."""
        if self.pg:
            import psycopg
            return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15)
        return sqlite3.connect(SQLITE_PATH)

    def _integrity_errors(self):
        if self.pg:
            import psycopg
            return (psycopg.errors.UniqueViolation,)
        return (sqlite3.IntegrityError,)

    def _rows(self, sql, params=()):
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(zip(COLUMNS, r)) for r in rows]

    def add(self, *, start: datetime, length_min: int, hold: timedelta, amount_kobo: int,
            mode, office, name, email, phone, matter_type, description, page_url,
            consent_text) -> dict:
        now = iso(now_utc())
        row = {
            "token": secrets.token_urlsafe(24), "created_at": now, "updated_at": now,
            "status": "pending_payment", "start_at": iso(start),
            "end_at": iso(start + timedelta(minutes=length_min)), "mode": mode,
            "office": office, "name": name, "email": email, "phone": phone,
            "matter_type": matter_type, "description": description, "page_url": page_url,
            "consent_text": consent_text, "consent_at": now,
            "hold_expires_at": iso(now_utc() + hold), "amount_kobo": amount_kobo,
        }
        cols = ", ".join(row)
        marks = ", ".join([self.ph] * len(row))
        try:
            with self._connect() as conn:
                if self.pg:
                    row["id"] = conn.execute(
                        f"INSERT INTO {TABLE} ({cols}) VALUES ({marks}) RETURNING id",
                        tuple(row.values())).fetchone()[0]
                else:
                    row["id"] = conn.execute(f"INSERT INTO {TABLE} ({cols}) VALUES ({marks})",
                                             tuple(row.values())).lastrowid
        except self._integrity_errors() as exc:
            raise SlotTaken() from exc
        return self.get(row["id"])

    def get(self, booking_id: int) -> dict | None:
        rows = self._rows(f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE id = {self.ph}",
                          (booking_id,))
        return rows[0] if rows else None

    def by_token(self, token: str) -> dict | None:
        rows = self._rows(f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE token = {self.ph}",
                          (token,))
        return rows[0] if rows else None

    def recent(self, limit=100) -> list[dict]:
        return self._rows(f"SELECT {', '.join(COLUMNS)} FROM {TABLE} "
                          f"ORDER BY start_at DESC LIMIT {self.ph}", (limit,))

    def held_starts(self, start: datetime, end: datetime) -> set[str]:
        active = ", ".join([self.ph] * len(ACTIVE))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT start_at FROM {TABLE} WHERE status IN ({active}) "
                f"AND start_at >= {self.ph} AND start_at < {self.ph}",
                (*ACTIVE, iso(start), iso(end))).fetchall()
        return {r[0] for r in rows}

    def move(self, booking_id: int, from_statuses: tuple[str, ...], to_status: str,
             **fields) -> dict | None:
        """Change status only if the booking is still in one of from_statuses.

        Returns the updated booking, or None if it had already moved on (a webhook
        and a status check racing to mark the same payment, say). Raises SlotTaken
        if reviving an expired booking would double-book its slot.
        """
        fields = {"status": to_status, "updated_at": iso(now_utc()), **fields}
        sets = ", ".join(f"{k} = {self.ph}" for k in fields)
        states = ", ".join([self.ph] * len(from_statuses))
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    f"UPDATE {TABLE} SET {sets} WHERE id = {self.ph} AND status IN ({states})",
                    (*fields.values(), booking_id, *from_statuses))
                changed = cur.rowcount > 0
        except self._integrity_errors() as exc:
            raise SlotTaken() from exc
        return self.get(booking_id) if changed else None

    def set(self, booking_id: int, **fields) -> None:
        """Update fields without touching the status (calendar ids, payment refs)."""
        fields = {"updated_at": iso(now_utc()), **fields}
        sets = ", ".join(f"{k} = {self.ph}" for k in fields)
        with self._connect() as conn:
            conn.execute(f"UPDATE {TABLE} SET {sets} WHERE id = {self.ph}",
                         (*fields.values(), booking_id))

    def expire_stale(self, now: datetime) -> list[dict]:
        """Release holds that ran out. Returns the bookings that just expired."""
        due = self._rows(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE status IN ({self.ph}, {self.ph}) "
            f"AND hold_expires_at < {self.ph}",
            ("pending_payment", "transfer_pending", iso(now)))
        expired = []
        for b in due:
            moved = self.move(b["id"], ("pending_payment", "transfer_pending"), "expired")
            if moved:
                expired.append(moved)
        return expired

    def delete(self, booking_id: int) -> bool:
        """Erase one booking (an NDPA erasure request)."""
        with self._connect() as conn:
            return conn.execute(f"DELETE FROM {TABLE} WHERE id = {self.ph}",
                                (booking_id,)).rowcount > 0
