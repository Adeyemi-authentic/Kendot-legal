"""Tiny Voyage AI client over plain HTTP (no SDK).

Why HTTP instead of the `voyageai` package: the SDK has no working release for
Python 3.14 (this venv), so we talk to Voyage's REST endpoint directly with
`requests`. An "embedding API" is just: send text -> get back a list of numbers.

Reused by every Week-2 day. Import it from a day folder like this:

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from voyage import embed, rerank

Reads VOYAGE_API_KEY from the project .env.
"""

import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

_BASE = "https://api.voyageai.com/v1"
# One keep-alive session: a fresh TLS handshake per call added about a second
# to each call (Phase 5: 1.5s -> 0.45s), and every question makes two calls.
_session = requests.Session()

# Total seconds _post may spend backing off on 429s. None = keep retrying (builds,
# eval). The live API sets a short cap so a visitor gets a "busy" message in a few
# seconds instead of a spinner for minutes (the free tier allows ~3 requests/min).
_max_wait = None


class VoyageBusy(RuntimeError):
    """Voyage kept rate-limiting us past the allowed wait."""


def set_max_wait(seconds):
    global _max_wait
    _max_wait = seconds


def _key() -> str:
    key = os.getenv("VOYAGE_API_KEY")
    if not key:
        raise SystemExit("VOYAGE_API_KEY not found. Check your .env file.")
    return key


def _post(path, payload, timeout, max_retries=8):
    """POST with automatic backoff on 429 AND transient connection drops.

    The free tier is both rate-limited (429) and occasionally drops the
    connection mid-request (RemoteDisconnected / read timeout). Both are
    retryable with the same exponential backoff -- a flaky upstream must not
    crash the pipeline.
    """
    headers = {
        "Authorization": f"Bearer {_key()}",
        "Content-Type": "application/json",
    }
    waited = 0.0
    for attempt in range(max_retries):
        last = attempt == max_retries - 1
        try:
            resp = _session.post(f"{_BASE}/{path}", headers=headers, json=payload, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            if last:
                raise
            wait = min(2 ** attempt + 1, 60.0)
            print(f"  (connection error: {type(e).__name__}; waiting {wait:.0f}s then retrying...)")
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            # Respect Retry-After if given, else exponential backoff capped at 60s.
            wait = min(float(resp.headers.get("Retry-After", 2 ** attempt + 1)), 60.0)
            if last or (_max_wait is not None and waited + wait > _max_wait):
                raise VoyageBusy("Voyage rate limit")
            print(f"  (rate limited; waiting {wait:.0f}s then retrying...)")
            time.sleep(wait)
            waited += wait
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()  # exhausted retries


def embed(texts, input_type, model="voyage-3.5-lite", timeout=30,
          max_batch_tokens=7000, max_batch_size=128):
    """Turn a list of strings into a list of vectors (each a list of floats).

    texts:      list[str] to embed.
    input_type: "document" for things you store/search, "query" for a user's
                question at search time. Voyage tunes the vector based on this.
    model:      embedding model id. Default voyage-3.5-lite (cheapest, 1024 dims).

    Sends texts in batches bounded by an approximate token budget (chars/4) AND a
    max count, so a large corpus does not exceed Voyage's per-request / per-minute
    token limits (which return 429). Batches go out in order and the results are
    concatenated in order, so the caller still gets one vector per input text.
    The per-request 429/connection backoff in _post paces requests that bunch up.

    Returns (vectors, total_tokens).
    """
    if isinstance(texts, str):
        texts = [texts]

    vectors = []
    total_tokens = 0
    i = 0
    while i < len(texts):
        batch, batch_tokens = [], 0
        while i < len(texts):
            approx = len(texts[i]) // 4 + 1
            if batch and (batch_tokens + approx > max_batch_tokens or len(batch) >= max_batch_size):
                break
            batch.append(texts[i])
            batch_tokens += approx
            i += 1
        body = _post(
            "embeddings",
            {"input": batch, "model": model, "input_type": input_type},
            timeout,
        )
        # data comes back in the same order we sent it; sort by index to be safe.
        rows = sorted(body["data"], key=lambda d: d["index"])
        vectors.extend(row["embedding"] for row in rows)
        total_tokens += body.get("usage", {}).get("total_tokens", 0)
    return vectors, total_tokens


def rerank(query, documents, model="rerank-2-lite", top_k=None, timeout=30):
    """Re-score a query against candidate documents (used on Day 6).

    Returns a list of dicts: {"index": i, "document": str, "relevance_score": float},
    best first. `index` points back into the original `documents` list.
    """
    payload = {"query": query, "documents": documents, "model": model}
    if top_k is not None:
        payload["top_k"] = top_k

    body = _post("rerank", payload, timeout)
    return sorted(body["data"], key=lambda d: -d["relevance_score"])
