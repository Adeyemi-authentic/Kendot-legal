"""Kendot Legal website assistant -- FastAPI service behind the chat widget.

    GET    /health              liveness + warm-up ping (the widget calls it on page load)
    POST   /chat                one-shot cited answer (JSON)                  public
    POST   /chat/stream         token-by-token SSE + citations                public
    POST   /intake              lawyer-handoff enquiry form                   public
    GET    /booking/options     fee, offices, payment methods, form wording   public
    GET    /booking/slots       free consultation slots                       public
    POST   /bookings            book a slot (held while the visitor pays)     public
    GET    /bookings/{token}    one booking's status (checks Paystack)        booking token
    POST   /bookings/{token}/pay            open a Paystack checkout          booking token
    POST   /bookings/{token}/transfer-sent  "I've paid by direct transfer"    booking token
    POST   /payments/paystack/webhook       payment confirmations            Paystack signature
    GET    /admin/bookings      bookings, newest slot first                   X-API-Key
    POST   /admin/bookings/{id}/mark-paid|confirm|cancel                      X-API-Key
    DELETE /admin/bookings/{id} erase one booking (NDPA erasure request)      X-API-Key
    GET    /admin/leads         recent enquiries                              X-API-Key
    DELETE /admin/leads/{id}    erase one enquiry (NDPA erasure request)      X-API-Key
    POST   /internal/login      password -> signed session token (8 hours)    INTERNAL_PASSWORD
    GET    /internal/session    is this token still valid?                    Bearer token
    POST   /internal/chat/stream  the firm's internal documents, streamed     Bearer token

The public routes are called from visitors' browsers, so they cannot hold a
secret: any key in the widget would be visible to everyone. They are protected
instead by:
  1. CORS allowlist        -- only the firm's own site can call them from a browser
  2. per-IP rate limits    -- separate limits for chat and for the enquiry form
  3. input caps            -- question length, history length, form field lengths
  4. daily spending cap    -- once the day's estimated AI cost hits the budget,
                              chat returns 503 and points visitors to the contact page
Admin routes keep API-key auth.

The internal assistant (rag/internal.py) answers from the firm's own precedents
and procedures. It has its own store, opened as a separate engine object; the
public routes only ever use app.state.engine, so they cannot reach it. Its routes
need a session token from /internal/login, and are switched off (404) unless
INTERNAL_PASSWORD is set.

The retrieval store is chosen by ENGINE: qdrant (local files, dev) or pg (pgvector
over DATABASE_URL, production). Leads follow the same switch (SQLite or Postgres).

Run:  uvicorn app:app --reload --port 8000        (from the app/ folder)
"""

import hashlib
import hmac
import json
import os
import pathlib
import secrets
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote

import anthropic
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator, model_validator

# --- Wire in the RAG engine (../rag) ---------------------------------------
HERE = pathlib.Path(__file__).resolve().parent                 # .../app
ROOT = HERE.parent                                             # api/
sys.path.insert(0, str(ROOT / "rag"))
load_dotenv(ROOT / ".env")                                     # no-op in the container

from chat import (                                             # noqa: E402
    answer, finish, gated_result, messages_for, cost_of,
    SYSTEM, MODEL, THRESHOLD, TOP_K, VOYAGE_PER_QUERY, MANIFEST, FIRM,
)
from contextualize import contextualize                        # noqa: E402
from leads import LeadStore                                    # noqa: E402
from voyage_client import VoyageBusy, set_max_wait            # noqa: E402
import internal                                                # noqa: E402
import bookings as bk                                          # noqa: E402
from gcal import CalendarError, make_calendar                  # noqa: E402
from payments import PaymentError, make_paystack, settled      # noqa: E402
from whatsapp import make_alerts                               # noqa: E402

ENGINE = os.environ.get("ENGINE", "qdrant").lower()
if ENGINE == "pg":
    from pg_engine import PgRetrievalEngine as Engine          # noqa: E402
else:
    from engine import RetrievalEngine as Engine               # noqa: E402


# --- Config (all from env) -------------------------------------------------
def _env_list(name, default):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


API_KEY = os.environ["API_KEY"]                                # admin routes only
ALLOWED_ORIGINS = _env_list(
    "ALLOWED_ORIGINS", "http://localhost:4321,http://127.0.0.1:4321")
CHAT_RATE_LIMIT = int(os.environ.get("CHAT_RATE_LIMIT", "10"))       # per IP ...
CHAT_RATE_WINDOW = float(os.environ.get("CHAT_RATE_WINDOW", "60"))   # ... per N seconds
INTAKE_RATE_LIMIT = int(os.environ.get("INTAKE_RATE_LIMIT", "5"))
INTAKE_RATE_WINDOW = float(os.environ.get("INTAKE_RATE_WINDOW", "3600"))
DAILY_BUDGET_USD = float(os.environ.get("DAILY_BUDGET_USD", "1.00"))
VOYAGE_MAX_WAIT = float(os.environ.get("VOYAGE_MAX_WAIT", "8"))   # seconds of 429 backoff per call
# Behind Render/Vercel the client IP arrives in X-Forwarded-For. Only trust that
# header when a proxy we control sets it, or anyone could spoof their IP.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
# Internal assistant. No password set = the internal routes do not exist (404).
INTERNAL_PASSWORD = os.environ.get("INTERNAL_PASSWORD", "")
INTERNAL_ENABLED = bool(INTERNAL_PASSWORD)
# Signs session tokens. Falls back to a key derived from API_KEY, so a deploy
# that forgets it still works; set it to sign out every session at once.
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET") or hmac.new(
    API_KEY.encode(), b"internal-session", hashlib.sha256).hexdigest()
INTERNAL_SESSION_HOURS = float(os.environ.get("INTERNAL_SESSION_HOURS", "8"))
INTERNAL_DAILY_BUDGET_USD = float(os.environ.get("INTERNAL_DAILY_BUDGET_USD", "1.00"))
LOGIN_RATE_LIMIT = int(os.environ.get("LOGIN_RATE_LIMIT", "5"))        # attempts per IP ...
LOGIN_RATE_WINDOW = float(os.environ.get("LOGIN_RATE_WINDOW", "900"))  # ... per 15 minutes
FIRM_WHATSAPP = "".join(ch for ch in os.environ.get("FIRM_WHATSAPP", "+234 700 000 0003")
                        if ch.isdigit())
# Consultation bookings (see bookings.py for the status flow).
SITE_URL = os.environ.get("SITE_URL", "http://localhost:4321").rstrip("/")
BOOKING_FEE_NGN = int(os.environ.get("BOOKING_FEE_NGN", "50000"))
BOOKING_HOLD = timedelta(minutes=float(os.environ.get("BOOKING_HOLD_MINUTES", "30")))
TRANSFER_HOLD = timedelta(hours=float(os.environ.get("TRANSFER_HOLD_HOURS", "24")))
BOOKING_RATE_LIMIT = int(os.environ.get("BOOKING_RATE_LIMIT", "5"))      # bookings per IP ...
BOOKING_RATE_WINDOW = float(os.environ.get("BOOKING_RATE_WINDOW", "3600"))  # ... per hour
# "City=address|City=address". The keys are what the form offers for in-person meetings.
OFFICES = dict(part.split("=", 1) for part in os.environ.get(
    "FIRM_OFFICES", "Lagos=12 Kendot Close, Lekki Phase 1, Lagos|"
                    "Abuja=Suite 4, Kendot House, Wuse 2, Abuja").split("|") if "=" in part)
BANK = {k: os.environ.get(f"FIRM_BANK_{k.upper()}", v) for k, v in {
    "name": "Demo Bank", "account_name": "Kendot Legal (demo)", "account_number": "0000000000"}.items()}

MAX_QUESTION = 500
MAX_HISTORY_TURNS = 6
CONSENT_TEXT = (
    f"I agree that {FIRM} may use these details to respond to my enquiry, "
    "as described in the privacy notice."
)
BOOKING_CONSENT_TEXT = (
    f"I agree that {FIRM} may use these details to arrange my consultation, including "
    "adding my name and email to the firm's Google Calendar invite, as described in the privacy notice."
)
# Matter types come from the synced practice pages, so a client build needs no edit here.
MATTER_TYPES = sorted(e["id"] for e in MANIFEST.values() if e["kind"] == "practice") + ["other"]


# --- Schemas ---------------------------------------------------------------
class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=1000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    history: list[Turn] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, v):
        if not v.strip():
            raise ValueError("Question is empty.")
        return v.strip()


class Citation(BaseModel):
    n: int
    title: str
    url: str | None
    quote: str


class Source(BaseModel):
    title: str
    url: str | None


class ChatResponse(BaseModel):
    answer: str
    refused: bool
    handoff: bool
    gated: bool
    top_score: float
    coverage: float
    flagged: bool
    citations: list[Citation]
    sources: list[Source]


class IntakeRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: str | None = Field(default=None, max_length=200,
                              pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str | None = Field(default=None, max_length=30, pattern=r"^\+?[0-9 ()-]{7,30}$")
    matter_type: str
    description: str = Field(min_length=10, max_length=2000)
    page_url: str | None = Field(default=None, max_length=300)
    consent: bool
    # Honeypot: a field hidden from people in the widget. Bots fill every field.
    website: str | None = None

    @field_validator("email", "phone", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        return (v.strip() or None) if isinstance(v, str) else v

    @field_validator("matter_type")
    @classmethod
    def _known_matter(cls, v):
        if v not in MATTER_TYPES:
            raise ValueError(f"matter_type must be one of: {', '.join(MATTER_TYPES)}")
        return v

    @model_validator(mode="after")
    def _checks(self):
        if not (self.email or self.phone):
            raise ValueError("Please give an email address or a phone number.")
        if not self.consent:
            raise ValueError("Consent is required so we can use your details to reply.")
        return self


class BookingRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str = Field(max_length=30, pattern=r"^\+?[0-9 ()-]{7,30}$")
    matter_type: str
    description: str = Field(min_length=10, max_length=2000)
    mode: Literal["virtual", "in_person"]
    office: str | None = None
    start: str = Field(max_length=40)                  # a start time from /booking/slots
    page_url: str | None = Field(default=None, max_length=300)
    consent: bool
    website: str | None = None                         # honeypot, as on /intake

    @field_validator("name", "email", "phone", "description", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("matter_type")
    @classmethod
    def _known_matter(cls, v):
        if v not in MATTER_TYPES:
            raise ValueError(f"matter_type must be one of: {', '.join(MATTER_TYPES)}")
        return v

    @model_validator(mode="after")
    def _checks(self):
        if self.mode == "in_person" and self.office not in OFFICES:
            raise ValueError(f"Please choose an office: {', '.join(OFFICES)}.")
        if self.mode == "virtual":
            self.office = None
        if not self.consent:
            raise ValueError("Consent is required so we can arrange your consultation.")
        return self


class CancelRequest(BaseModel):
    reason: str = Field(default="", max_length=300)


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class IntakeResponse(BaseModel):
    ok: bool
    reference: str
    message: str
    whatsapp_url: str


# --- Errors ----------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# --- Lock: admin API key ---------------------------------------------------
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_admin(key: str | None = Depends(_api_key_header)) -> None:
    if not key or not secrets.compare_digest(key, API_KEY):
        raise ApiError(401, "Missing or invalid API key.")


# --- Lock: per-IP rate limits ---------------------------------------------
# In-memory, so correct for ONE process. Multiple instances would share a store
# (e.g. Redis). IPs are only held in memory, hashed, and never written to disk.
_hits: dict[tuple[str, str], deque[float]] = {}


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(bucket_name: str, limit: int, window: float):
    def dep(request: Request) -> None:
        ip = hashlib.sha256(client_ip(request).encode()).hexdigest()[:16]
        now = time.monotonic()
        bucket = _hits.setdefault((bucket_name, ip), deque())
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            raise ApiError(429, "Too many requests. Please wait a moment and try again.")
        bucket.append(now)
    return dep


# --- Lock: daily spending cap ---------------------------------------------
class Budget:
    """Estimated USD spent on AI calls today (UTC). Resets at midnight UTC.

    In-memory: a restart resets the count, which errs on the side of serving
    visitors; the provider-side monthly limit is the hard backstop.
    """

    def __init__(self, limit):
        self.limit = limit
        self.day = None
        self.spent = 0.0

    def _roll(self):
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day, self.spent = today, 0.0

    def check(self) -> None:
        self._roll()
        if self.spent >= self.limit:
            raise ApiError(503, "The assistant is resting for today. Please use the contact "
                                "page and a lawyer will get back to you.")

    def add(self, usd: float) -> None:
        self._roll()
        self.spent += usd


budget = Budget(DAILY_BUDGET_USD)
internal_budget = Budget(INTERNAL_DAILY_BUDGET_USD)    # lawyers are not starved by public traffic
chat_guard = rate_limit("chat", CHAT_RATE_LIMIT, CHAT_RATE_WINDOW)
intake_guard = rate_limit("intake", INTAKE_RATE_LIMIT, INTAKE_RATE_WINDOW)
login_guard = rate_limit("login", LOGIN_RATE_LIMIT, LOGIN_RATE_WINDOW)
booking_guard = rate_limit("booking", BOOKING_RATE_LIMIT, BOOKING_RATE_WINDOW)
booking_step_guard = rate_limit("booking-step", 20, 3600)   # pay / "I've sent a transfer"
booking_read_guard = rate_limit("booking-read", 60, 60)     # slot lists and status checks
internal_chat_guard = rate_limit("internal-chat", CHAT_RATE_LIMIT, CHAT_RATE_WINDOW)


def chat_gate(_rl: None = Depends(chat_guard)) -> None:
    """Rate limit first (cheap), then the spending cap."""
    budget.check()


# --- Lock: internal sessions ----------------------------------------------
# A token is "<expiry>.<HMAC of the expiry>". Stateless: nothing to store, and
# the password's hash is in the signed message, so changing INTERNAL_PASSWORD
# signs everyone out.
def _sign(expiry: int) -> str:
    pw = hashlib.sha256(INTERNAL_PASSWORD.encode()).hexdigest()
    return hmac.new(INTERNAL_SECRET.encode(), f"internal:{expiry}:{pw}".encode(),
                    hashlib.sha256).hexdigest()


def issue_token() -> tuple[str, int]:
    expiry = int(time.time() + INTERNAL_SESSION_HOURS * 3600)
    return f"{expiry}.{_sign(expiry)}", expiry


def internal_on() -> None:
    if not INTERNAL_ENABLED:
        raise ApiError(404, "Not Found")


def require_internal(_on: None = Depends(internal_on),
                     authorization: str | None = Header(default=None)) -> None:
    token = (authorization or "").removeprefix("Bearer ").strip()
    expiry, _, sig = token.partition(".")
    if not (expiry.isdigit() and secrets.compare_digest(sig, _sign(int(expiry)))
            and int(expiry) > time.time()):
        raise ApiError(401, "Please sign in again.")


def internal_gate(_auth: None = Depends(require_internal),
                  _rl: None = Depends(internal_chat_guard)) -> None:
    internal_budget.check()


# --- App -------------------------------------------------------------------
def open_internal_engine():
    """The internal store, or None (switched off, or not built yet).

    A missing internal index must never take the public assistant down with it.
    """
    if not INTERNAL_ENABLED:
        return None
    try:
        eng = internal.make_engine(ENGINE)
        eng._load()
        print(f"[startup] internal store: {eng.collection}", file=sys.stderr)
        return eng
    except (Exception, SystemExit) as exc:          # noqa: BLE001
        print(f"[WARN] internal assistant unavailable: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[startup] retrieval store: {ENGINE}", file=sys.stderr)
    set_max_wait(VOYAGE_MAX_WAIT)       # live requests: fail fast to a "busy" message
    app.state.engine = Engine()
    app.state.engine._load()            # build the BM25 cache now, not on the first question
    app.state.client = anthropic.Anthropic()
    app.state.leads = LeadStore()
    app.state.bookings = bk.BookingStore()
    app.state.calendar = make_calendar(FIRM)
    app.state.paystack = make_paystack()
    app.state.alerts = make_alerts()
    app.state.internal_engine = open_internal_engine()
    yield
    app.state.engine.close()
    if app.state.internal_engine:
        app.state.internal_engine.close()


app = FastAPI(title=f"{FIRM} website assistant", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
)


@app.exception_handler(ApiError)
async def _api_error(request: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    # One readable message for the widget to show, plus which field it is about.
    first = exc.errors()[0] if exc.errors() else {}
    msg = str(first.get("msg", "Invalid request.")).removeprefix("Value error, ")
    field = ".".join(str(x) for x in first.get("loc", ())[1:]) or None
    return JSONResponse(status_code=422, content={"error": msg, "field": field})


BUSY_MSG = ("The assistant is busy right now. Please try again in a minute, "
            "or use the contact page to reach a lawyer.")


@app.exception_handler(VoyageBusy)
async def _voyage_busy(request: Request, exc: VoyageBusy):
    return JSONResponse(status_code=503, content={"error": BUSY_MSG})


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
    return JSONResponse(status_code=500, content={"error": "Internal server error."})


@app.get("/")
def root():
    return {"service": f"{FIRM} website assistant", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    return {"status": "ok", "engine": ENGINE}


# --- Chat ------------------------------------------------------------------
def standalone_query(req: ChatRequest, meter=budget.add) -> str:
    """Rewrite a follow-up into a standalone query using the recent turns."""
    history = [(t.role, t.text) for t in req.history[-MAX_HISTORY_TURNS:]]
    return contextualize(app.state.client, history, req.question, meter=meter)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, _gate: None = Depends(chat_gate)):
    query = standalone_query(req)
    r = answer(app.state.engine, app.state.client, query, meter=budget.add)
    return ChatResponse(
        answer=r["text"], refused=r["refused"], handoff=r["handoff"], gated=r["gated"],
        top_score=r["top_score"], coverage=r["coverage"], flagged=r["flagged"],
        citations=r["citations"], sources=r["sources"],
    )


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _done(r: dict) -> str:
    return _sse({"type": "done", **{k: r[k] for k in (
        "refused", "handoff", "gated", "top_score", "coverage", "flagged",
        "citations", "sources")}})


def public_profile(client):
    """How the website assistant gates, prompts, finishes and meters."""
    return dict(
        gate=lambda q, score: gated_result(client, q, score, meter=budget.add),
        request=lambda q, passages: dict(model=MODEL, max_tokens=1024, system=SYSTEM,
                                         messages=messages_for(q, passages)),
        finish=finish, meter=budget.add,
    )


INTERNAL_PROFILE = dict(
    gate=lambda q, score: internal.gated_result(score),
    request=internal.generate_kwargs,
    finish=internal.finish_internal, meter=internal_budget.add,
)


def stream_pipeline(engine, client, query, profile=None):
    """Same decisions as chat.answer(), streamed. Errors become an SSE event,
    because once streaming starts the HTTP status can no longer change."""
    p = profile or public_profile(client)
    try:
        passages = engine.search(query, k=TOP_K)
        p["meter"](VOYAGE_PER_QUERY)
        top_score = passages[0][1] if passages else 0.0

        if top_score < THRESHOLD:
            r = p["gate"](query, top_score)
            yield _sse({"type": "token", "text": r["text"]})
            yield _done(r)
            return

        with client.messages.stream(**p["request"](query, passages)) as stream:
            streamed = ""
            for text in stream.text_stream:
                streamed += text
                yield _sse({"type": "token", "text": text})
            final = stream.get_final_message()
        p["meter"](cost_of(final.usage))
        r = p["finish"](final, passages, top_score)
        if not streamed.strip():
            yield _sse({"type": "token", "text": r["text"]})   # empty reply -> DONT_KNOW
        yield _done(r)
    except VoyageBusy:
        print("[WARN] stream: Voyage rate limit", file=sys.stderr)
        yield _sse({"type": "error", "error": BUSY_MSG})
    except Exception as exc:                       # noqa: BLE001 -- report, don't crash the stream
        print(f"[ERROR] stream: {type(exc).__name__}: {exc}", file=sys.stderr)
        yield _sse({"type": "error", "error": "Sorry, something went wrong. Please try again."})


@app.post("/chat/stream")
def chat_stream(req: ChatRequest, _gate: None = Depends(chat_gate)):
    query = standalone_query(req)
    return StreamingResponse(
        stream_pipeline(app.state.engine, app.state.client, query),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Intake (lawyer handoff) ----------------------------------------------
def wa_link(text: str, number: str = FIRM_WHATSAPP) -> str:
    return f"https://wa.me/{number}?text={quote(text)}"


def whatsapp_url(reference: str, matter_type: str) -> str:
    return wa_link(f"Hello {FIRM}, I have just sent an enquiry (reference {reference}) "
                   f"about {matter_type.replace('-', ' ')}.")


@app.post("/intake", response_model=IntakeResponse)
def intake(req: IntakeRequest, _rl: None = Depends(intake_guard)):
    if req.website:
        # A bot filled the hidden field. Look successful, store nothing.
        return IntakeResponse(ok=True, reference="KL-0", message="Thank you.",
                              whatsapp_url=whatsapp_url("KL-0", req.matter_type))
    lead_id = app.state.leads.add(
        name=req.name.strip(), email=req.email, phone=req.phone,
        matter_type=req.matter_type, description=req.description.strip(),
        page_url=req.page_url, consent_text=CONSENT_TEXT,
    )
    reference = f"KL-{lead_id:04d}"
    return IntakeResponse(
        ok=True, reference=reference,
        message=(f"Thank you. Your reference is {reference}. A lawyer will reply within "
                 "one working day. For anything urgent, message us on WhatsApp."),
        whatsapp_url=whatsapp_url(reference, req.matter_type),
    )


@app.get("/intake/options")
def intake_options():
    """What the widget's form needs: matter types and the exact consent wording."""
    return {"matter_types": MATTER_TYPES, "consent_text": CONSENT_TEXT}


# --- Consultation bookings -----------------------------------------------
# The firm's calendar is asked for free/busy at most once a minute; a new booking
# clears the cache so the slot it took disappears at once.
_busy_cache: dict = {"at": 0.0, "busy": []}


def naira(kobo: int) -> str:
    return f"₦{kobo // 100:,}"


def admin_url() -> str:
    return f"{SITE_URL}/admin/bookings/"


def alert(tasks: BackgroundTasks, text: str) -> None:
    """Send a WhatsApp alert to the firm after the response has gone."""
    tasks.add_task(app.state.alerts.send, text)


def sweep() -> None:
    """Release holds that ran out, and take them off the firm's calendar."""
    for b in app.state.bookings.expire_stale(bk.now_utc()):
        if b["calendar_event_id"]:
            try:
                app.state.calendar.delete(b["calendar_event_id"], notify=False)
                app.state.bookings.set(b["id"], calendar_event_id=None)
            except CalendarError as exc:
                print(f"[WARN] could not remove expired hold {bk.reference(b['id'])}: {exc}",
                      file=sys.stderr)


def free_slots() -> list[datetime]:
    sched = bk.Schedule()
    now = bk.now_utc()
    window_end = now + timedelta(days=sched.days_ahead + 1)
    if time.monotonic() - _busy_cache["at"] > 60:
        try:
            _busy_cache["busy"] = app.state.calendar.busy(now, window_end)
        except Exception as exc:                        # noqa: BLE001
            print(f"[ERROR] calendar free/busy: {exc}", file=sys.stderr)
            raise ApiError(503, "Online booking is unavailable right now. Please message us "
                                "on WhatsApp or use the contact page.") from exc
        _busy_cache["at"] = time.monotonic()
    held = app.state.bookings.held_starts(now, window_end)
    return sched.free(now, _busy_cache["busy"], held)


def hold_note(status: str) -> str:
    return {"pending_payment": "awaiting payment",
            "transfer_pending": "transfer to check",
            "paid": "PAID, conflict check then confirm"}.get(status, status)


def place_hold(b: dict) -> None:
    """Put (or re-title) the booking's private hold on the firm's calendar.

    A calendar failure must not lose the booking: the database row is what holds
    the slot, so we log and carry on, and Confirm creates the event if it is missing.
    """
    cal = app.state.calendar
    try:
        if b["calendar_event_id"]:
            cal.retitle(b["calendar_event_id"], b, hold_note(b["status"]))
        else:
            event_id = cal.hold(b, hold_note(b["status"]))
            if event_id:
                app.state.bookings.set(b["id"], calendar_event_id=event_id)
                b["calendar_event_id"] = event_id
    except CalendarError as exc:
        print(f"[WARN] calendar hold for {bk.reference(b['id'])}: {exc}", file=sys.stderr)


def describe(b: dict) -> str:
    """One line for alerts: 'BK-0042, Tue 14 Oct, 10:00 WAT, virtual, property (Ada Obi)'."""
    where = "virtual" if b["mode"] == "virtual" else f"in person, {b['office']}"
    return (f"{bk.reference(b['id'])}, {bk.label(bk.parse(b['start_at']))}, {where}, "
            f"{b['matter_type'].replace('-', ' ')} ({b['name']})")


def status_message(b: dict) -> str:
    until = bk.label(bk.parse(b["hold_expires_at"])) if b["hold_expires_at"] else ""
    fee = naira(b["amount_kobo"])
    return {
        "pending_payment": (f"Booked, subject to confirmation. Your slot is held until {until}. "
                            f"Pay the {fee} consultation fee to keep it."),
        "transfer_pending": (f"Thank you. We will check our account for your transfer and update "
                             f"you. Your slot is held until {until}."),
        "paid": ("Payment received. Your booking is subject to confirmation: a lawyer will run a "
                 "conflict check and email you the calendar invite within one working day."),
        "confirmed": (f"Confirmed. The calendar invite has been sent to {b['email']}."
                      + (" It includes the Google Meet link." if b["meet_url"] else "")),
        "cancelled": "This booking has been cancelled. We will contact you about any refund.",
        "expired": ("The hold on this slot ran out before payment arrived. "
                    "Please book a new time."),
        "paid_slot_taken": ("Payment received, but your slot was taken while the payment was "
                            "processing. We will contact you to rebook or refund."),
    }.get(b["status"], "")


def visitor_view(b: dict) -> dict:
    """What the visitor's browser may see: no description, no internal ids."""
    start = bk.parse(b["start_at"])
    ref = bk.reference(b["id"])
    return {
        "reference": ref, "token": b["token"], "status": b["status"],
        "message": status_message(b), "start": b["start_at"], "start_label": bk.label(start),
        "mode": b["mode"], "office": b["office"], "address": OFFICES.get(b["office"] or ""),
        "fee": naira(b["amount_kobo"]), "hold_expires_at": b["hold_expires_at"],
        "meet_url": b["meet_url"] if b["status"] == "confirmed" else None,
        "can_pay": b["status"] in ("pending_payment", "transfer_pending"),
        "paystack": app.state.paystack is not None,
        "bank": {**BANK, "narration": ref},
        "whatsapp_url": wa_link(f"Hello {FIRM}, I have just booked a consultation "
                                f"(reference {ref}) for {bk.label(start)}."),
    }


def mark_paid(b: dict, method: str, pay_ref: str | None, tasks: BackgroundTasks) -> dict:
    """Record a payment once, from whichever route sees it first."""
    store = app.state.bookings
    fields = {"payment_method": method, "payment_ref": pay_ref or b["payment_ref"],
              "paid_at": bk.iso(bk.now_utc()), "hold_expires_at": None}
    try:
        moved = store.move(b["id"], ("pending_payment", "transfer_pending", "expired"),
                           "paid", **fields)
    except bk.SlotTaken:
        moved = store.move(b["id"], ("expired",), "paid_slot_taken", **fields)
        if moved:
            alert(tasks, f"Late payment, slot taken: {describe(moved)}. Paid "
                         f"{naira(moved['amount_kobo'])} by {method}. Rebook or refund. {admin_url()}")
        return moved or store.get(b["id"])
    if not moved:
        return store.get(b["id"])               # someone else recorded it already
    place_hold(moved)
    alert(tasks, f"Paid {naira(moved['amount_kobo'])} ({method}): {describe(moved)}. "
                 f"Run the conflict check, then confirm or cancel: {admin_url()}")
    return moved


def check_paystack(b: dict, tasks: BackgroundTasks) -> dict:
    """If the visitor has been to checkout, ask Paystack whether it went through.

    Covers a slow or missed webhook (and local development, where Paystack cannot
    reach the API at all).
    """
    ps = app.state.paystack
    if not (ps and b["payment_ref"] and b["status"] in ("pending_payment", "transfer_pending", "expired")):
        return b
    try:
        data = ps.verify(b["payment_ref"])
    except PaymentError as exc:
        print(f"[WARN] Paystack verify {b['payment_ref']}: {exc}", file=sys.stderr)
        return b
    return mark_paid(b, "paystack", b["payment_ref"], tasks) if settled(data, b["amount_kobo"]) else b


def booking_by_token(token: str) -> dict:
    b = app.state.bookings.by_token(token) if len(token) <= 64 else None
    if not b:
        raise ApiError(404, "Booking not found.")
    return b


@app.get("/booking/options")
def booking_options():
    """What the booking page needs to draw the form."""
    sched = bk.Schedule()
    return {"matter_types": MATTER_TYPES, "consent_text": BOOKING_CONSENT_TEXT,
            "fee": naira(BOOKING_FEE_NGN * 100), "length_minutes": sched.length,
            "offices": [{"city": c, "address": a} for c, a in OFFICES.items()],
            "paystack": app.state.paystack is not None, "bank": BANK,
            "hold_minutes": int(BOOKING_HOLD.total_seconds() // 60)}


@app.get("/booking/slots", dependencies=[Depends(booking_read_guard)])
def booking_slots():
    """Free start times, grouped by day (in WAT)."""
    sweep()
    days: dict[str, dict] = {}
    for s in free_slots():
        local = s.astimezone(bk.WAT)
        day = days.setdefault(local.date().isoformat(), {
            "date": local.date().isoformat(), "label": f"{local:%A} {local.day} {local:%B}",
            "slots": []})
        day["slots"].append({"start": bk.iso(s), "label": f"{local:%H:%M}"})
    return {"timezone": "WAT (UTC+1)", "days": list(days.values())}


@app.post("/bookings")
def create_booking(req: BookingRequest, tasks: BackgroundTasks,
                   _rl: None = Depends(booking_guard)):
    if req.website:
        raise ApiError(409, "That time has just been taken. Please pick another.")
    try:
        start = bk.parse(req.start)
    except ValueError as exc:
        raise ApiError(422, "Please pick a time from the list.") from exc
    sweep()
    if start not in free_slots():
        raise ApiError(409, "That time has just been taken. Please pick another.")
    try:
        b = app.state.bookings.add(
            start=start, length_min=bk.Schedule().length, hold=BOOKING_HOLD,
            amount_kobo=BOOKING_FEE_NGN * 100, mode=req.mode, office=req.office,
            name=req.name, email=req.email, phone=req.phone, matter_type=req.matter_type,
            description=req.description, page_url=req.page_url,
            consent_text=BOOKING_CONSENT_TEXT)
    except bk.SlotTaken as exc:
        raise ApiError(409, "That time has just been taken. Please pick another.") from exc
    _busy_cache["at"] = 0.0
    place_hold(b)
    alert(tasks, f"New booking, awaiting payment: {describe(b)}. {admin_url()}")
    return visitor_view(b)


@app.get("/bookings/{token}", dependencies=[Depends(booking_read_guard)])
def get_booking(token: str, tasks: BackgroundTasks):
    sweep()
    return visitor_view(check_paystack(booking_by_token(token), tasks))


@app.post("/bookings/{token}/pay", dependencies=[Depends(booking_step_guard)])
def pay_booking(token: str):
    sweep()
    b = booking_by_token(token)
    ps = app.state.paystack
    if not ps:
        raise ApiError(404, "Online payment is not available. Please pay by bank transfer.")
    if b["status"] not in ("pending_payment", "transfer_pending"):
        raise ApiError(409, status_message(b))
    ref = bk.reference(b["id"])
    try:
        pay_ref, url = ps.start(
            email=b["email"], amount_kobo=b["amount_kobo"], booking_ref=ref,
            callback_url=f"{SITE_URL}/book/?b={b['token']}",
            metadata={"booking_id": b["id"], "custom_fields": [
                {"display_name": "Booking", "variable_name": "booking", "value": ref}]})
    except PaymentError as exc:
        print(f"[ERROR] Paystack: {exc}", file=sys.stderr)
        raise ApiError(502, "We could not open the payment page. Please try again, "
                            "or pay by bank transfer.") from exc
    app.state.bookings.set(b["id"], payment_ref=pay_ref)
    return {"authorization_url": url}


@app.post("/bookings/{token}/transfer-sent", dependencies=[Depends(booking_step_guard)])
def transfer_sent(token: str, tasks: BackgroundTasks):
    sweep()
    b = booking_by_token(token)
    moved = app.state.bookings.move(
        b["id"], ("pending_payment",), "transfer_pending", payment_method="transfer",
        # Never hold past the consultation itself.
        hold_expires_at=bk.iso(min(bk.now_utc() + TRANSFER_HOLD, bk.parse(b["start_at"]))))
    if not moved:
        if b["status"] == "transfer_pending":
            return visitor_view(b)
        raise ApiError(409, status_message(b))
    place_hold(moved)
    alert(tasks, f"Transfer to check: {describe(moved)} says they sent "
                 f"{naira(moved['amount_kobo'])} with narration {bk.reference(moved['id'])}. "
                 f"Mark it paid when it lands: {admin_url()}")
    return visitor_view(moved)


@app.post("/payments/paystack/webhook")
async def paystack_webhook(request: Request, tasks: BackgroundTasks):
    ps = app.state.paystack
    raw = await request.body()
    if not ps or not ps.signed(raw, request.headers.get("x-paystack-signature")):
        raise ApiError(401, "Bad signature.")
    event = json.loads(raw or b"{}")
    data = event.get("data") or {}
    if event.get("event") != "charge.success":
        return {"ok": True}
    booking_id = (data.get("metadata") or {}).get("booking_id")
    b = app.state.bookings.get(int(booking_id)) if str(booking_id).isdigit() else None
    if b and settled(data, b["amount_kobo"]):
        mark_paid(b, "paystack", data.get("reference"), tasks)
    return {"ok": True}


# --- Admin -----------------------------------------------------------------
@app.get("/admin/leads", dependencies=[Depends(require_admin)])
def admin_leads(limit: int = 50):
    return {"leads": app.state.leads.recent(min(max(limit, 1), 500)),
            "spent_today_usd": round(budget.spent, 4), "daily_budget_usd": budget.limit}


@app.delete("/admin/leads/{lead_id}", dependencies=[Depends(require_admin)])
def admin_delete_lead(lead_id: int):
    if not app.state.leads.delete(lead_id):
        raise ApiError(404, "No such lead.")
    return {"deleted": lead_id}


def admin_view(b: dict) -> dict:
    return {**b, "reference": bk.reference(b["id"]),
            "start_label": bk.label(bk.parse(b["start_at"])),
            "fee": naira(b["amount_kobo"]),
            "contact": {"email": f"mailto:{b['email']}",
                        "whatsapp": wa_link(f"Hello {b['name']}, this is {FIRM} about your "
                                            f"consultation booking {bk.reference(b['id'])}.",
                                            "".join(c for c in b["phone"] if c.isdigit()))}}


def admin_booking(booking_id: int) -> dict:
    b = app.state.bookings.get(booking_id)
    if not b:
        raise ApiError(404, "No such booking.")
    return b


@app.get("/admin/bookings", dependencies=[Depends(require_admin)])
def admin_bookings(limit: int = 100):
    sweep()
    return {"bookings": [admin_view(b) for b in
                         app.state.bookings.recent(min(max(limit, 1), 500))]}


@app.post("/admin/bookings/{booking_id}/mark-paid", dependencies=[Depends(require_admin)])
def admin_mark_paid(booking_id: int, tasks: BackgroundTasks):
    """The firm has seen a direct transfer land in its account."""
    b = admin_booking(booking_id)
    if b["status"] not in ("pending_payment", "transfer_pending", "expired"):
        raise ApiError(409, f"This booking is {b['status'].replace('_', ' ')}.")
    return admin_view(mark_paid(b, b["payment_method"] or "transfer", None, tasks))


@app.post("/admin/bookings/{booking_id}/confirm", dependencies=[Depends(require_admin)])
def admin_confirm(booking_id: int):
    """Conflict check passed: send the visitor the calendar invite (and Meet link)."""
    b = admin_booking(booking_id)
    if b["status"] != "paid":
        raise ApiError(409, "Only paid bookings can be confirmed.")
    cal = app.state.calendar
    try:
        event_id = b["calendar_event_id"] or cal.hold(b, hold_note("paid"))
        meet = cal.confirm(event_id, b, OFFICES.get(b["office"] or "")) if event_id else None
    except CalendarError as exc:
        print(f"[ERROR] confirm {bk.reference(b['id'])}: {exc}", file=sys.stderr)
        raise ApiError(502, "Google Calendar refused the invite. Nothing was sent; "
                            "please try again.") from exc
    moved = app.state.bookings.move(b["id"], ("paid",), "confirmed",
                                    calendar_event_id=event_id, meet_url=meet)
    if not moved:
        raise ApiError(409, "This booking changed while you were confirming it.")
    return admin_view(moved)


@app.post("/admin/bookings/{booking_id}/cancel", dependencies=[Depends(require_admin)])
def admin_cancel(booking_id: int, req: CancelRequest):
    """Cancel (a conflict of interest, say). A confirmed visitor gets Google's
    cancellation email; anyone else is contacted with the links in the response."""
    b = admin_booking(booking_id)
    cancellable = (*bk.ACTIVE, "paid_slot_taken")
    if b["status"] not in cancellable:
        raise ApiError(409, f"This booking is {b['status'].replace('_', ' ')}.")
    if b["calendar_event_id"]:
        try:
            app.state.calendar.delete(b["calendar_event_id"], notify=b["status"] == "confirmed")
        except CalendarError as exc:
            print(f"[WARN] cancel {bk.reference(b['id'])}: {exc}", file=sys.stderr)
    moved = app.state.bookings.move(b["id"], cancellable, "cancelled",
                                    calendar_event_id=None, cancel_reason=req.reason.strip() or None)
    _busy_cache["at"] = 0.0
    return admin_view(moved or admin_booking(booking_id))


@app.delete("/admin/bookings/{booking_id}", dependencies=[Depends(require_admin)])
def admin_delete_booking(booking_id: int):
    b = admin_booking(booking_id)
    if b["calendar_event_id"]:
        try:
            app.state.calendar.delete(b["calendar_event_id"], notify=b["status"] == "confirmed")
        except CalendarError as exc:
            print(f"[WARN] erase {bk.reference(b['id'])}: {exc}", file=sys.stderr)
    app.state.bookings.delete(booking_id)
    _busy_cache["at"] = 0.0
    return {"deleted": booking_id}


# --- Internal assistant (the firm's lawyers only) --------------------------
@app.post("/internal/login", dependencies=[Depends(internal_on), Depends(login_guard)])
def internal_login(req: LoginRequest):
    if not secrets.compare_digest(req.password.encode(), INTERNAL_PASSWORD.encode()):
        raise ApiError(401, "Wrong password.")
    token, expiry = issue_token()
    return {"token": token, "expires_at": expiry}


@app.get("/internal/session", dependencies=[Depends(require_internal)])
def internal_session():
    return {"ok": True, "ready": app.state.internal_engine is not None}


@app.post("/internal/chat/stream")
def internal_chat_stream(req: ChatRequest, _gate: None = Depends(internal_gate)):
    engine = app.state.internal_engine
    if engine is None:
        raise ApiError(503, "The internal assistant is not set up yet.")
    query = standalone_query(req, meter=internal_budget.add)
    return StreamingResponse(
        stream_pipeline(engine, app.state.client, query, INTERNAL_PROFILE),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
