"""Enquiry ("lead") storage for the assistant's lawyer-handoff form.

Follows the same switch as retrieval: ENGINE=pg stores leads in Postgres
(DATABASE_URL, the same Neon database as the index); anything else uses a local
SQLite file, so the form works in development with no database to set up.

NDPA data minimisation: we keep only what the firm needs to reply (name, a
contact route, the matter, a description, the page it came from) plus a record
of the consent the visitor gave. No IP address or browser data is stored.

    python leads.py          # list the most recent leads (admin view)
"""

import os
import pathlib
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

HERE = pathlib.Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env")

TABLE = os.environ.get("LEADS_TABLE", "kendot_leads")
SQLITE_PATH = HERE / "leads.sqlite"
COLUMNS = ("id", "created_at", "name", "email", "phone", "matter_type",
           "description", "page_url", "consent_text", "consent_at")


class LeadStore:
    def __init__(self):
        self.pg = os.environ.get("ENGINE", "qdrant").lower() == "pg"
        self.ph = "%s" if self.pg else "?"     # parameter placeholder per driver
        with self._connect() as conn:
            id_col = ("id SERIAL PRIMARY KEY" if self.pg
                      else "id INTEGER PRIMARY KEY AUTOINCREMENT")
            ts = "TIMESTAMPTZ" if self.pg else "TEXT"
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                    {id_col},
                    created_at   {ts} NOT NULL,
                    name         TEXT NOT NULL,
                    email        TEXT,
                    phone        TEXT,
                    matter_type  TEXT NOT NULL,
                    description  TEXT NOT NULL,
                    page_url     TEXT,
                    consent_text TEXT NOT NULL,
                    consent_at   {ts} NOT NULL
                )
            """)

    def _connect(self):
        """A short-lived connection per call: enquiries are rare, and a fresh
        connection never trips over Neon reaping an idle one."""
        if self.pg:
            import psycopg
            return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15)
        return sqlite3.connect(SQLITE_PATH)

    def add(self, *, name, email, phone, matter_type, description, page_url, consent_text):
        """Store one enquiry and return its reference number."""
        now = datetime.now(timezone.utc)
        if not self.pg:
            now = now.isoformat(timespec="seconds")
        values = (now, name, email, phone, matter_type, description, page_url, consent_text, now)
        cols = ", ".join(COLUMNS[1:])
        marks = ", ".join([self.ph] * len(values))
        with self._connect() as conn:
            if self.pg:
                row = conn.execute(
                    f"INSERT INTO {TABLE} ({cols}) VALUES ({marks}) RETURNING id", values
                ).fetchone()
                return row[0]
            return conn.execute(f"INSERT INTO {TABLE} ({cols}) VALUES ({marks})", values).lastrowid

    def recent(self, limit=50):
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM {TABLE} ORDER BY id DESC LIMIT {self.ph}",
                (limit,),
            ).fetchall()
        return [{k: (v.isoformat() if isinstance(v, datetime) else v)
                 for k, v in zip(COLUMNS, row)} for row in rows]

    def delete(self, lead_id):
        """Erase one lead (an NDPA erasure request). Returns True if it existed."""
        with self._connect() as conn:
            cur = conn.execute(f"DELETE FROM {TABLE} WHERE id = {self.ph}", (lead_id,))
            return cur.rowcount > 0


def main():
    for lead in LeadStore().recent():
        print(f"#{lead['id']}  {lead['created_at']}  {lead['matter_type']:<22} "
              f"{lead['name']} <{lead['email'] or lead['phone']}>")
        print(f"    {lead['description'][:100]}")


if __name__ == "__main__":
    main()
