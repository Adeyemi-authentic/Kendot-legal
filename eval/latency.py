"""Time to first streamed token, measured against the running API (TARGETS.md row 8).

Sends one warm-up question, then 10 answerable questions to /chat/stream and
times each from sending the request to the first `token` event. Requests are
spaced out to stay under the per-IP chat rate limit (10 per minute by default).

Start the API first (from api/app):  ..\\.venv\\Scripts\\python -m uvicorn app:app --port 8000

    python eval/latency.py [base_url] [spacing_seconds]

Voyage's free tier (no payment method) allows about 3 requests a minute and each
question makes two, so on the free tier pass 45 to measure latency rather than
rate-limit backoff. With a paid Voyage account the default spacing is fine.
"""

import json
import pathlib
import statistics
import sys
import time

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"   # not localhost: on Windows it tries IPv6 first (+2s)
GOLDEN = pathlib.Path(__file__).resolve().parent / "golden.jsonl"
TARGET_S = 3.0
SPACING_S = float(sys.argv[2]) if len(sys.argv) > 2 else 7.0   # API limit: 10/min
N = 10


def first_token_seconds(question):
    """(seconds to first token, seconds to done) for one streamed question."""
    start = time.perf_counter()
    first = None
    with requests.post(f"{BASE}/chat/stream", json={"question": question},
                       stream=True, timeout=60) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event["type"] == "token" and first is None:
                first = time.perf_counter() - start
            elif event["type"] == "error":
                raise RuntimeError(event["error"])
            elif event["type"] == "done":
                break
    return first, time.perf_counter() - start


def main():
    rows = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    questions = [r["question"] for r in rows if r["category"] == "answerable"][:N]
    requests.get(f"{BASE}/health", timeout=60).raise_for_status()
    first_token_seconds("What are your office hours?")            # warm-up, not timed
    ttfts = []
    for q in questions:
        time.sleep(SPACING_S)
        ttft, total = first_token_seconds(q)
        ttfts.append(ttft)
        print(f"  first token {ttft:5.2f}s   done {total:5.2f}s   {q[:60]}")
    median = statistics.median(ttfts)
    met = median < TARGET_S
    print(f"\n  {'MET ' if met else 'MISS'}  median time to first token {median:.2f}s "
          f"(p90 {sorted(ttfts)[int(0.9 * len(ttfts)) - 1]:.2f}s)   target < {TARGET_S:.0f}s")
    sys.exit(0 if met else 1)


if __name__ == "__main__":
    main()
