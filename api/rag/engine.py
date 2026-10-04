"""Semantic search engine over company documents (Week-2 capstone).

End-to-end retrieval pipeline:

    ingest docs -> smart chunk -> embed (Voyage) -> store (Qdrant local)
                -> hybrid retrieve (BM25 + dense, fused by RRF)
                -> rerank (Voyage cross-encoder) -> top passages with source + score

This is the retrieval CORE that a RAG app (generation on top) will wrap later.
Self-contained: needs only voyage_client.py, qdrant-client, rank-bm25, numpy, and
a VOYAGE_API_KEY in a .env file.

CLI:
    python engine.py build        # chunk+embed+store the docs/ folder (once)
    python engine.py build --rebuild   # drop and re-index after a content sync
    python engine.py "my question"   # search and print reranked top passages
"""

import json
import os
import pathlib
import re
import sys

from qdrant_client import QdrantClient, models
from rank_bm25 import BM25Okapi

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from voyage_client import embed, rerank
from ingest import load_documents

DOCS_DIR = HERE / "docs"
STORE_PATH = HERE / "index_data"
# One index per firm. Override with COLLECTION for a client build.
COLLECTION = os.environ.get("COLLECTION", "kendot_site")
VECTOR_SIZE = 1024          # voyage-3.5-lite
# Website prose is short-paragraph and list-heavy. 600 keeps a heading with its
# list or answer as one chunk while keeping the embedding signal focused.
CHUNK_SIZE = 600
OVERLAP = 0.15
RRF_K = 60


# --- ingest -----------------------------------------------------------------

def _split_long(para, size):
    """Break a single paragraph longer than `size` on sentence boundaries.

    A statutory subsection can run to thousands of characters; without this a
    single paragraph would refuse to split (the packer only breaks BETWEEN
    paragraphs) and become one oversized chunk. Sentence-split first, then
    hard-cut any lone sentence that is still absurdly long.
    """
    if len(para) <= size:
        return [para]
    sentences = re.split(r"(?<=[.;:])\s+", para)
    pieces, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > size:
            pieces.append(cur.strip())
            cur = s
        else:
            cur = (cur + " " + s) if cur else s
    if cur.strip():
        pieces.append(cur.strip())
    out = []
    for p in pieces:
        while len(p) > size * 1.5:
            out.append(p[:size])
            p = p[size:]
        if p.strip():
            out.append(p)
    return out


def smart_chunks(text, size=CHUNK_SIZE, overlap=OVERLAP):
    """Paragraph-aware chunking with ~overlap carry-over (see Day 3).

    Oversized single paragraphs are pre-split on sentence boundaries so one long
    provision cannot become a single giant chunk.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        for piece in _split_long(para, size):
            if current and len(current) + len(piece) + 2 > size:
                chunks.append(current.strip())
                tail = current[-int(size * overlap):]
                tail = tail[tail.find(" ") + 1:]   # start the overlap on a word boundary
                current = tail + "\n\n" + piece
            else:
                current = (current + "\n\n" + piece) if current else piece
    if current.strip():
        chunks.append(current.strip())
    return chunks


def tokenize(text):
    """Lowercase alphanumeric tokens; keeps codes/IDs (e.g. 0x0000011b) whole."""
    return re.findall(r"[a-z0-9]+", text.lower())


def load_manifest(docs_dir=DOCS_DIR):
    """sources.json from scripts/sync_content.py: filename -> {title, url, ...}."""
    path = pathlib.Path(docs_dir) / "sources.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def build_records(docs_dir=DOCS_DIR):
    """Chunk every doc into (source, chunk_text) records, shared by both stores.

    Each chunk is prefixed with its page title (when it doesn't already start
    with it), so a chunk from the middle of a page still says what page it is
    about: "From: Frequently Asked Questions" helps both BM25 and the embedding.
    """
    manifest = load_manifest(docs_dir)
    records = []
    docs = list(load_documents(docs_dir))
    for name, text in docs:
        title = manifest.get(name, {}).get("title")
        for chunk in smart_chunks(text):
            if title and not chunk.startswith(title):
                chunk = f"From: {title}\n\n{chunk}"
            records.append((name, chunk))
    return records, len(docs)


# --- engine -----------------------------------------------------------------

class RetrievalEngine:
    def __init__(self, store_path=STORE_PATH, collection=COLLECTION):
        self.client = QdrantClient(path=str(store_path))
        self.collection = collection
        self._ids = self._sources = self._texts = self._bm25 = None

    def close(self):
        self.client.close()

    # ingest --------------------------------------------------------------
    def build(self, docs_dir=DOCS_DIR, rebuild=False):
        """Chunk + embed + store every doc in docs_dir.

        Idempotent unless rebuild=True, which drops the index first (run it after
        scripts/sync_content.py so new or edited pages are picked up).
        """
        if self.client.collection_exists(self.collection):
            if not rebuild:
                n = self.client.count(self.collection).count
                print(f"Index already built: '{self.collection}' has {n} chunks. "
                      "Use --rebuild to re-index.")
                return
            self.client.delete_collection(self.collection)
            print(f"Dropped '{self.collection}' for rebuild.")

        records, n_docs = build_records(docs_dir)
        texts = [c for _s, c in records]
        print(f"Embedding {len(texts)} chunks from {n_docs} docs...")
        vectors, tokens = embed(texts, input_type="document")
        print(f"  embedded ({tokens} tokens).")

        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=models.Distance.COSINE),
        )
        self.client.upsert(self.collection, points=[
            models.PointStruct(id=i, vector=v, payload={"source": s, "chunk_text": c})
            for i, ((s, c), v) in enumerate(zip(records, vectors))
        ])
        print(f"Stored {len(records)} chunks in '{self.collection}'.")

    # corpus cache --------------------------------------------------------
    def _load(self):
        if self._ids is not None:
            return
        pts, _ = self.client.scroll(self.collection, limit=10000, with_payload=True)
        pts.sort(key=lambda p: p.id)
        self._ids = [p.id for p in pts]
        self._sources = {p.id: p.payload["source"] for p in pts}
        self._texts = {p.id: p.payload["chunk_text"] for p in pts}
        self._bm25 = BM25Okapi([tokenize(self._texts[i]) for i in self._ids])

    def source(self, pid):
        self._load(); return self._sources[pid]

    def text(self, pid):
        self._load(); return self._texts[pid]

    # retrieval stages ----------------------------------------------------
    def _dense(self, query):
        qv, _ = embed([query], input_type="query")
        res = self.client.query_points(self.collection, query=qv[0], limit=10000)
        return [h.id for h in res.points]

    def _bm25_rank(self, query):
        self._load()
        scores = self._bm25.get_scores(tokenize(query))
        return [pid for pid, _ in sorted(zip(self._ids, scores), key=lambda p: -p[1])]

    def hybrid(self, query, top_n=10):
        """Stage 1: dense + BM25 fused by RRF; return top_n point ids."""
        self._load()
        rankings = [self._dense(query), self._bm25_rank(query)]
        fused = {}
        for ranking in rankings:
            for rank, pid in enumerate(ranking, start=1):
                fused[pid] = fused.get(pid, 0.0) + 1.0 / (RRF_K + rank)
        ordered = [pid for pid, _ in sorted(fused.items(), key=lambda x: -x[1])]
        return ordered[:top_n]

    def search(self, query, top_n=25, k=5, do_rerank=True):
        """Full pipeline. Returns [(id, score, source, text), ...] best-first.

        score is the reranker's relevance (do_rerank=True) or the stage-1 rank
        position's RRF order (do_rerank=False, score is just descending rank).
        """
        self._load()
        candidates = self.hybrid(query, top_n=top_n)
        if not do_rerank:
            return [(pid, None, self._sources[pid], self._texts[pid]) for pid in candidates[:k]]

        docs = [self._texts[pid] for pid in candidates]
        # Score every candidate (same cost: Voyage bills the documents sent, not returned).
        reranked = rerank(query, docs, top_k=len(docs))
        picked = reranked[:k]
        # Anchor: always keep the stage-1 winner. When dense and BM25 agree on a
        # chunk, the lite reranker can still bury it under a shorter, vaguer one
        # (Phase 5: the "we don't handle divorce" FAQ was hybrid #1, rerank #9).
        if candidates and all(r["index"] != 0 for r in picked):
            picked.append(next(r for r in reranked if r["index"] == 0))
        return [
            (candidates[r["index"]], r["relevance_score"],
             self._sources[candidates[r["index"]]], self._texts[candidates[r["index"]]])
            for r in picked
        ]


def _flat(text, n=110):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n] + "..."


def main():
    args = sys.argv[1:]
    engine = RetrievalEngine()
    try:
        if not args or args[0] == "build":
            engine.build(rebuild="--rebuild" in args)
            if not args:
                print('\nUsage: python engine.py "your question"')
            return

        query = " ".join(args)
        print(f'Query: "{query}"\n')
        for rank, (pid, score, source, text) in enumerate(engine.search(query), 1):
            print(f"#{rank}  rel={score:0.4f}  [{source}]")
            print(f"    {_flat(text)}")
    finally:
        engine.close()


if __name__ == "__main__":
    main()
