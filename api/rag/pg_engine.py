"""Week-4 Day 3: the same retrieval engine, with pgvector (Neon) as the store.

The point of this file is what it does NOT contain. The Week-3 engine's
hybrid fusion, BM25, RRF, and reranking are inherited UNCHANGED. We override
only the three methods that touch storage:

    __init__  -- open a Postgres/pgvector connection instead of Qdrant
    build     -- create the table + ingest chunks into pgvector
    _load     -- read all rows back (for the BM25 cache + id->text/source maps)
    _dense    -- nearest-neighbour search with the `<=>` cosine operator
    close     -- close the connection

Everything above the store (hybrid(), search(), rerank) calls these methods
through the same interface, so it neither knows nor cares that Qdrant became
Postgres. That is the whole lesson: a clean boundary lets you replace the
database without touching the system built on top of it.

    python pg_engine.py build          # create table + ingest into Neon (once)
    python pg_engine.py "my question"  # search via pgvector and print top passages
"""

import os
import pathlib
import sys

import numpy as np
import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector
from rank_bm25 import BM25Okapi

HERE = pathlib.Path(__file__).resolve().parent                 # ...\rag
ROOT = HERE.parent                                             # repo root
sys.path.insert(0, str(HERE))
load_dotenv(ROOT / ".env")

# Reuse the EXACT retrieval engine + its ingest helpers. We subclass the engine
# and reuse its chunking, tokenizer, dimensions, and docs folder verbatim, so
# the chunks here are byte-identical to the ones Qdrant holds -> fair parity.
from engine import (                                            # noqa: E402
    RetrievalEngine, build_records, tokenize,
    DOCS_DIR, VECTOR_SIZE,
)
from voyage_client import embed                                 # noqa: E402

# One table per firm, so several demos can share one Neon database.
TABLE = os.environ.get("CHUNKS_TABLE", "kendot_chunks")


class PgRetrievalEngine(RetrievalEngine):
    """RetrievalEngine backed by pgvector instead of Qdrant.

    Only storage methods are overridden; hybrid()/search()/rerank are inherited.
    """

    def __init__(self, dsn=None, table=TABLE):
        self.dsn = dsn or os.environ["DATABASE_URL"]
        self.table = table
        self._open()
        self.collection = table
        # same corpus-cache slots the parent uses (filled lazily by _load)
        self._ids = self._sources = self._texts = self._bm25 = None

    def _open(self):
        """(Re)establish the connection, ensure the vector type, register it.

        Isolated so build() can reconnect after a long embedding pause -- a
        managed pooler (Neon) reaps a connection left idle, so we do not trust
        the same socket to survive minutes of Voyage calls.
        """
        # connect_timeout so a DNS/network blip fails fast instead of hanging
        # (Neon is remote; the free tier occasionally drops a lookup).
        # autocommit: a read must not leave the long-lived connection idle in an
        # open transaction -- Neon terminates it (IdleInTransactionSessionTimeout)
        # and the next question fails. Writes use an explicit transaction.
        self.conn = psycopg.connect(self.dsn, connect_timeout=15, autocommit=True)
        with self.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        self.conn.commit()
        register_vector(self.conn)          # lets psycopg bind numpy arrays as vectors

    def _reconnect(self):
        try:
            self.conn.close()
        except psycopg.Error:
            pass
        self._open()

    def _ensure_live(self):
        """Ping the connection; reconnect if the pooler dropped it while idle."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1")
            self.conn.commit()
        except psycopg.Error:
            self._reconnect()

    def _query(self, sql, params=None):
        """Run a read query, reconnecting once if the pooled connection was reaped.

        A managed pooler (Neon, especially the free tier) closes a connection left
        idle between requests; the first query afterward then fails with
        'server closed the connection unexpectedly'. Retrying once on a fresh
        connection makes retrieval resilient to that in the deployed app.

        The server can also end the session with a non-operational error (e.g.
        IdleInTransactionSessionTimeout is an InternalError), so retry whenever
        the connection is left broken, whatever the error class.
        """
        for attempt in (1, 2):
            try:
                with self.conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall()
            except psycopg.Error as e:
                if attempt == 2 or not (isinstance(e, psycopg.OperationalError) or self.conn.broken):
                    raise
                self._reconnect()

    def close(self):
        self.conn.close()

    # ingest --------------------------------------------------------------
    def build(self, docs_dir=DOCS_DIR, rebuild=False):
        """Create the chunks table and ingest every doc in docs_dir. Idempotent.

        Chunks are produced by the SAME smart_chunks() Qdrant used and inserted
        with explicit 0-based ids in the same order, so ids line up 1:1 across
        both stores -- which makes the parity check apples-to-apples.

        CRITICAL ORDERING: embed BEFORE opening the write transaction. Embedding a
        large corpus takes minutes; a managed pooler (Neon) terminates a
        connection left idle-in-transaction, so holding a transaction open across
        the Voyage calls would kill the build. So: create table + count + commit,
        release the transaction, embed with no transaction held, then insert in
        one quick burst (reconnecting first in case the idle socket was reaped).
        """
        with self.conn.cursor() as cur:
            if rebuild:
                cur.execute(f"DROP TABLE IF EXISTS {self.table}")
                print(f"Dropped '{self.table}' for rebuild.")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    id          INT PRIMARY KEY,
                    source      TEXT NOT NULL,
                    chunk_text  TEXT NOT NULL,
                    embedding   vector({VECTOR_SIZE})
                )
            """)
            cur.execute(f"SELECT count(*) FROM {self.table}")
            existing = cur.fetchone()[0]
        self.conn.commit()          # release the transaction before the long embed
        if existing:
            print(f"Index already built: '{self.table}' has {existing} chunks.")
            return

        records, n_docs = build_records(docs_dir)   # same chunks as the Qdrant build
        texts = [c for _s, c in records]
        print(f"Embedding {len(texts)} chunks from {n_docs} docs...")
        vectors, tokens = embed(texts, input_type="document")   # no open transaction here
        print(f"  embedded ({tokens} tokens).")

        self._ensure_live()         # the idle connection may have been reaped during embed
        with self.conn.transaction(), self.conn.cursor() as cur:   # one all-or-nothing burst
            for i, ((source, chunk), vec) in enumerate(zip(records, vectors)):
                cur.execute(
                    f"INSERT INTO {self.table} (id, source, chunk_text, embedding) VALUES (%s, %s, %s, %s)",
                    (i, source, chunk, np.array(vec, dtype=np.float32)),
                )
        print(f"Stored {len(records)} chunks in pgvector table '{self.table}'.")
        # NOTE: no ANN index (e.g. HNSW) on purpose -- with ~50 chunks an exact
        # scan is instant AND gives EXACT nearest neighbours, which is what the
        # parity test needs. A production corpus would add an HNSW index here.

    # corpus cache --------------------------------------------------------
    def _load(self):
        """Pull all rows once: id->source, id->text, and the BM25 index.

        Identical role to the parent's Qdrant scroll; only the source changes.
        """
        if self._ids is not None:
            return
        rows = self._query(f"SELECT id, source, chunk_text FROM {self.table} ORDER BY id")
        self._ids = [r[0] for r in rows]
        self._sources = {r[0]: r[1] for r in rows}
        self._texts = {r[0]: r[2] for r in rows}
        self._bm25 = BM25Okapi([tokenize(self._texts[i]) for i in self._ids])

    # dense retrieval -----------------------------------------------------
    def _dense(self, query):
        """Embed the query and rank ALL chunks by cosine distance via `<=>`.

        Returns every id best-first (the parent's hybrid() needs the full
        ranking to fuse with BM25 via RRF). `<=>` = cosine distance, so the
        smallest distance = most similar, matching Qdrant's COSINE metric.
        """
        qv, _ = embed([query], input_type="query")
        rows = self._query(
            f"SELECT id FROM {self.table} ORDER BY embedding <=> %s",
            (np.array(qv[0], dtype=np.float32),),
        )
        return [r[0] for r in rows]


def _flat(text, n=110):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n] + "..."


def main():
    args = sys.argv[1:]
    engine = PgRetrievalEngine()
    try:
        if not args or args[0] == "build":
            engine.build(rebuild="--rebuild" in args)
            if not args:
                print('\nUsage: python pg_engine.py "your question"')
            return
        query = " ".join(args)
        print(f'Query: "{query}"  (via pgvector)\n')
        for rank, (pid, score, source, text) in enumerate(engine.search(query), 1):
            print(f"#{rank}  rel={score:0.4f}  [{source}]")
            print(f"    {_flat(text)}")
    finally:
        engine.close()


if __name__ == "__main__":
    main()
