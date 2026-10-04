"""Prove the two stores are interchangeable (SKILL: the store is swappable).

The pgvector engine inherits hybrid/RRF/rerank from the Qdrant engine and only
overrides storage, and both build from the SAME chunker with the SAME 0-based
ids. So for any query the retrieved chunks should line up. This runs a handful of
queries through both stores and reports, per query, the overlap of the top-k
chunk ids and whether the #1 result matches.

    python parity_check.py          (needs the local Qdrant index AND DATABASE_URL)
"""

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from engine import RetrievalEngine            # noqa: E402
from pg_engine import PgRetrievalEngine        # noqa: E402

QUERIES = [
    "what is the standard rate of VAT in Nigeria?",
    "how much notice must a landlord give a yearly tenant to quit in Lagos?",
    "what does the Constitution say about the right to life?",
    "what penalty applies to a taxable person who fails to register for tax?",
]
K = 5


def main():
    q_engine = RetrievalEngine()
    pg_engine = PgRetrievalEngine()
    try:
        total_overlap = top1_match = 0
        for q in QUERIES:
            q_hits = q_engine.search(q, k=K)
            pg_hits = pg_engine.search(q, k=K)
            q_ids = [h[0] for h in q_hits]
            pg_ids = [h[0] for h in pg_hits]
            overlap = len(set(q_ids) & set(pg_ids))
            same_top1 = q_ids[:1] == pg_ids[:1]
            total_overlap += overlap
            top1_match += same_top1
            print(f"[{overlap}/{K} overlap, top1 {'==' if same_top1 else '!='}]  {q}")
            print(f"    qdrant : {q_ids}  ({q_hits[0][2]})")
            print(f"    pgvec  : {pg_ids}  ({pg_hits[0][2]})")
        n = len(QUERIES)
        print("-" * 60)
        print(f"mean top-{K} id overlap : {total_overlap / n:.2f} / {K}")
        print(f"top-1 source matches   : {top1_match}/{n}")
    finally:
        q_engine.close()
        pg_engine.close()


if __name__ == "__main__":
    main()
