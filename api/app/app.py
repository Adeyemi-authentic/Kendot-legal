"""Kendot Legal website assistant -- FastAPI service behind the chat widget.

    GET    /health              liveness + warm-up ping (the widget calls it on page load)
    POST   /chat                one-shot cited answer (JSON)                  public
    POST   /chat/stream         token-by-token SSE + citations                public
    POST   /intake              lawyer-handoff enquiry form                   public
    GET    /admin/leads         recent enquiries                              X-API-Key
    DELETE /admin/leads/{id}    erase one enquiry (NDPA erasure request)      X-API-Key

The public routes are called from visitors' browsers, so they cannot hold a
secret: any key in the widget would be visible to everyone. They are protected
instead by:
  1. CORS allowlist        -- only the firm's own site can call them from a browser
  2. per-IP rate limits    -- separate limits for chat and for the enquiry form
  3. input caps            -- question length, history length, form field lengths
  4. daily spending cap    -- once the day's estimated AI cost hits the budget,
                              chat returns 503 and points visitors to the contact page
Admin routes keep API-key auth.

The retrieval store is chosen by ENGINE: qdrant (local files, dev) or pg (pgvector
over DATABASE_URL, production). Leads follow the same switch (SQLite or Postgres).

Run:  uvicorn app:app --reload --port 8000        (from the app/ folder)
"""

import hashlib
import json
import os
import pathlib
import secrets
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote

import anthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
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
# Behind Render/Vercel the client IP arrives in X-Forwarded-For. Only trust that
# header when a proxy we control sets it, or anyone could spoof their IP.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
FIRM_WHATSAPP = "".join(ch for ch in os.environ.get("FIRM_WHATSAPP", "+234 700 000 0003")
                        if ch.isdigit())

MAX_QUESTION = 500
MAX_HISTORY_TURNS = 6
CONSENT_TEXT = (
    f"I agree that {FIRM} may use these details to respond to my enquiry, "
    "as described in the privacy notice."
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
chat_guard = rate_limit("chat", CHAT_RATE_LIMIT, CHAT_RATE_WINDOW)
intake_guard = rate_limit("intake", INTAKE_RATE_LIMIT, INTAKE_RATE_WINDOW)


def chat_gate(_rl: None = Depends(chat_guard)) -> None:
    """Rate limit first (cheap), then the spending cap."""
    budget.check()


# --- App -------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[startup] retrieval store: {ENGINE}", file=sys.stderr)
    app.state.engine = Engine()
    app.state.engine._load()            # build the BM25 cache now, not on the first question
    app.state.client = anthropic.Anthropic()
    app.state.leads = LeadStore()
    yield
    app.state.engine.close()


app = FastAPI(title=f"{FIRM} website assistant", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
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
def standalone_query(req: ChatRequest) -> str:
    """Rewrite a follow-up into a standalone query using the recent turns."""
    history = [(t.role, t.text) for t in req.history[-MAX_HISTORY_TURNS:]]
    return contextualize(app.state.client, history, req.question, meter=budget.add)


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


def stream_pipeline(engine, client, query):
    """Same decisions as chat.answer(), streamed. Errors become an SSE event,
    because once streaming starts the HTTP status can no longer change."""
    try:
        passages = engine.search(query, k=TOP_K)
        budget.add(VOYAGE_PER_QUERY)
        top_score = passages[0][1] if passages else 0.0

        if top_score < THRESHOLD:
            r = gated_result(client, query, top_score, meter=budget.add)
            yield _sse({"type": "token", "text": r["text"]})
            yield _done(r)
            return

        with client.messages.stream(
            model=MODEL, max_tokens=1024, system=SYSTEM,
            messages=messages_for(query, passages),
        ) as stream:
            streamed = ""
            for text in stream.text_stream:
                streamed += text
                yield _sse({"type": "token", "text": text})
            final = stream.get_final_message()
        budget.add(cost_of(final.usage))
        r = finish(final, passages, top_score)
        if not streamed.strip():
            yield _sse({"type": "token", "text": r["text"]})   # empty reply -> DONT_KNOW
        yield _done(r)
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
def whatsapp_url(reference: str, matter_type: str) -> str:
    text = (f"Hello {FIRM}, I have just sent an enquiry (reference {reference}) "
            f"about {matter_type.replace('-', ' ')}.")
    return f"https://wa.me/{FIRM_WHATSAPP}?text={quote(text)}"


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
