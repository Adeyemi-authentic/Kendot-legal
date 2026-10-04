"""Check the golden set before any money is spent evaluating against it.

A wrong gold answer silently turns a correct reply into a "failure", so every
gold fact for an answerable question must actually appear on one of the pages
the question expects to be cited. Also checks structure, ids and category sizes.

    python scripts/validate_golden.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval" / "golden.jsonl"
DOCS = ROOT / "api" / "rag" / "docs"
sys.path.insert(0, str(ROOT / "api" / "rag"))
from ingest import load_documents                               # noqa: E402

ANSWERING = {"answerable", "paraphrase", "multiturn"}
CATEGORIES = ANSWERING | {"out_of_scope", "referral", "advice", "injection"}
OUTCOMES = {"answer", "refuse", "handoff"}
# Minimum rows per category, and the PLAN.md range for the whole set.
MINIMUMS = {"answerable": 10, "paraphrase": 2, "multiturn": 1, "out_of_scope": 4,
            "referral": 1, "advice": 4, "injection": 3}
TOTAL_RANGE = (30, 40)


def norm(text):
    return " ".join(text.lower().split())


def alternatives(fact):
    """A gold fact is one string or a list of accepted spellings."""
    return fact if isinstance(fact, list) else [fact]


def main():
    manifest = json.loads((DOCS / "sources.json").read_text(encoding="utf-8"))
    text_of = dict(load_documents(DOCS))
    page_text = {e["url"]: norm(text_of[name]) for name, e in manifest.items()}

    errors, counts, seen = [], {}, set()
    rows = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    for row in rows:
        rid = row.get("id", "?")
        where = f"{rid}:"
        if rid in seen:
            errors.append(f"{where} duplicate id")
        seen.add(rid)
        cat = row.get("category")
        if cat not in CATEGORIES:
            errors.append(f"{where} unknown category {cat!r}")
            continue
        counts[cat] = counts.get(cat, 0) + 1
        if not str(row.get("question", "")).strip():
            errors.append(f"{where} empty question")
        if len(row.get("question", "")) > 500:
            errors.append(f"{where} question longer than the API's 500-char cap")

        for turn in row.get("history", []):
            if len(turn) != 2 or turn[0] not in {"user", "assistant"}:
                errors.append(f"{where} bad history turn {turn!r}")

        if cat in ANSWERING or cat == "referral":
            if not row.get("gold"):
                errors.append(f"{where} needs gold facts")
        if cat in ANSWERING:
            urls = row.get("sources") or []
            if not urls:
                errors.append(f"{where} needs expected source pages")
            unknown = [u for u in urls if u not in page_text]
            if unknown:
                errors.append(f"{where} unknown source page(s) {unknown}")
            pages = [page_text[u] for u in urls if u in page_text]
            for fact in row.get("gold", []):
                if not any(norm(alt) in p for alt in alternatives(fact) for p in pages):
                    errors.append(f"{where} gold {fact!r} is on none of {urls}")
        if cat == "referral":
            # The referral wording must exist somewhere on the site to be quotable.
            for fact in row["gold"]:
                if not any(norm(alt) in p for alt in alternatives(fact) for p in page_text.values()):
                    errors.append(f"{where} referral gold {fact!r} is not on any page")
        if cat == "injection":
            accept = row.get("accept")
            if not accept or not set(accept) <= OUTCOMES:
                errors.append(f"{where} injection needs 'accept' from {sorted(OUTCOMES)}")
            if not isinstance(row.get("forbidden"), list):
                errors.append(f"{where} injection needs a 'forbidden' list (may be empty)")

    for cat, low in MINIMUMS.items():
        if counts.get(cat, 0) < low:
            errors.append(f"category {cat}: {counts.get(cat, 0)} rows, need at least {low}")
    lo, hi = TOTAL_RANGE
    if not lo <= len(rows) <= hi:
        errors.append(f"{len(rows)} rows; PLAN.md asks for {lo} to {hi}")

    print(f"{len(rows)} questions: " + ", ".join(f"{c} {n}" for c, n in sorted(counts.items())))
    if errors:
        print(f"\n{len(errors)} problem(s):")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    print("Golden set OK: every gold fact appears on an expected page.")


if __name__ == "__main__":
    main()
