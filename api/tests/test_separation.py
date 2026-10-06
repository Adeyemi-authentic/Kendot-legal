"""Phase 7: the public assistant cannot reach the firm's internal documents.

Three layers of proof, cheapest first:

    1. CORPUS  -- no internal text is in the public corpus folder, and every
                  internal document carries the KL/INT/ marker these tests look for.
    2. STORES  -- the built public index holds no internal chunk; the internal
                  index holds nothing else; the code refuses to point both at one store.
    3. ROUTES  -- through the real FastAPI app (fake engines record every search):
                  public routes only ever search the public store, whatever the
                  request carries; internal routes need a valid, unexpired token.

Offline and free: no Voyage or Anthropic calls. Optional live check (real
retrieval over the real public index, ~2 Voyage calls per question):

    RUN_LIVE=1 ../.venv/bin/python -m pytest -q tests/        (from api/)
"""

import json
import os
import pathlib
import sys
import time

import pytest

API = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API / "rag"))
sys.path.insert(0, str(API / "app"))
os.environ["INTERNAL_PASSWORD"] = "test-password"      # before app is imported
os.environ.setdefault("API_KEY", "test-admin-key")

import internal                                         # noqa: E402
from engine import COLLECTION, DOCS_DIR, STORE_PATH, load_manifest   # noqa: E402

MARKER = internal.MARKER
# Questions only the internal documents can answer.
INTERNAL_QUESTIONS = [
    "What liability cap do we start with in a share sale agreement?",
    "What are the firm's hourly rates for partners in 2026?",
    "Who sets the data protection duty rota?",
    "What is the conflict check procedure for a new matter?",
]


# --- 1. Corpus --------------------------------------------------------------
def test_every_internal_doc_carries_the_marker():
    docs = sorted(p for p in internal.INTERNAL_DOCS.iterdir() if p.suffix in {".md", ".txt"})
    assert docs, "no internal documents found"
    for path in docs:
        assert MARKER in path.read_text(encoding="utf-8"), path.name
        assert path.name in internal.MANIFEST, f"{path.name} missing from sources.json"


def test_public_corpus_holds_no_internal_text():
    for path in DOCS_DIR.glob("*"):
        if path.is_file():
            assert MARKER not in path.read_text(encoding="utf-8", errors="ignore"), path.name


def test_public_and_internal_manifests_do_not_overlap():
    public = load_manifest(DOCS_DIR)
    assert not set(public) & set(internal.MANIFEST)
    assert all(e.get("url") is None for e in internal.MANIFEST.values()), \
        "an internal document must never link to a public page"


# --- 2. Stores --------------------------------------------------------------
def _chunks(store_path, collection):
    from qdrant_client import QdrantClient
    if not store_path.exists():
        pytest.skip(f"{store_path.name} not built")
    try:
        client = QdrantClient(path=str(store_path))
    except RuntimeError as exc:                          # the local API holds the lock
        pytest.skip(f"store in use: {exc}")
    try:
        if not client.collection_exists(collection):
            pytest.skip(f"collection {collection} not built")
        points, _ = client.scroll(collection, limit=10000, with_payload=True)
        return [p.payload for p in points]
    finally:
        client.close()


def test_public_index_holds_no_internal_chunk():
    chunks = _chunks(STORE_PATH, COLLECTION)
    assert chunks
    for c in chunks:
        assert c["source"] not in internal.MANIFEST
        assert MARKER not in c["chunk_text"]


def test_internal_index_holds_only_internal_chunks():
    chunks = _chunks(internal.INTERNAL_STORE, internal.INTERNAL_COLLECTION)
    assert chunks
    assert {c["source"] for c in chunks} <= set(internal.MANIFEST)


def test_internal_store_cannot_be_the_public_store(monkeypatch):
    import pg_engine
    monkeypatch.setattr(internal, "INTERNAL_TABLE", pg_engine.TABLE)
    with pytest.raises(SystemExit):
        internal.make_engine("pg")
    monkeypatch.setattr(internal, "INTERNAL_COLLECTION", COLLECTION)
    with pytest.raises(SystemExit):
        internal.make_engine("qdrant")


# --- 3. Routes --------------------------------------------------------------
class FakeEngine:
    """Records every search. A low score sends the pipeline down the gated path,
    so no generation call is made."""

    def __init__(self, name):
        self.name, self.queries = name, []

    def search(self, query, k=5):
        self.queries.append(query)
        return [(0, 0.05, f"{self.name}.md", f"{self.name} text")]


class FakeClient:
    """Stands in for anthropic.Anthropic(): answers the gate's YES/NO check with NO."""

    class messages:                                       # noqa: N801
        @staticmethod
        def create(**_):
            class Usage: input_tokens = output_tokens = 1
            class Block: type, text = "text", "NO"
            class Resp: content, usage = [Block()], Usage()
            return Resp()


@pytest.fixture()
def api(monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module
    app_module._hits.clear()
    monkeypatch.setattr(app_module, "INTERNAL_ENABLED", True)
    public, private = FakeEngine("public"), FakeEngine("internal")
    app_module.app.state.engine = public
    app_module.app.state.internal_engine = private
    app_module.app.state.client = FakeClient()
    # No `with`: the lifespan (real stores, real clients) does not run.
    return TestClient(app_module.app), app_module, public, private


def _events(resp):
    return [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]


def _login(client):
    r = client.post("/internal/login", json={"password": "test-password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_public_chat_only_searches_the_public_store(api):
    client, _, public, private = api
    headers = _login(client)
    for q in INTERNAL_QUESTIONS:
        # Even a signed-in lawyer's token and a made-up "corpus" field change nothing.
        r = client.post("/chat/stream", headers=headers,
                        json={"question": q, "corpus": "internal"})
        assert r.status_code == 200
        assert MARKER not in r.text
    assert public.queries == INTERNAL_QUESTIONS
    assert private.queries == []


def test_internal_chat_only_searches_the_internal_store(api):
    client, _, public, private = api
    r = client.post("/internal/chat/stream", headers=_login(client),
                    json={"question": INTERNAL_QUESTIONS[0]})
    assert r.status_code == 200
    assert _events(r)[-1]["type"] == "done"
    assert private.queries == [INTERNAL_QUESTIONS[0]]
    assert public.queries == []


def test_internal_chat_needs_a_valid_token(api):
    client, app_module, public, private = api
    past = int(time.time()) - 60
    bad = [
        {},                                                              # none
        {"Authorization": "Bearer nonsense"},                            # malformed
        {"Authorization": f"Bearer {int(time.time()) + 3600}.{'0' * 64}"},   # forged
        {"Authorization": f"Bearer {past}.{app_module._sign(past)}"},    # expired
    ]
    for headers in bad:
        r = client.post("/internal/chat/stream", headers=headers,
                        json={"question": INTERNAL_QUESTIONS[0]})
        assert r.status_code == 401, headers
        assert client.get("/internal/session", headers=headers).status_code == 401
    assert private.queries == [] and public.queries == []


def test_wrong_password_is_refused_and_rate_limited(api):
    client, app_module, *_ = api
    codes = [client.post("/internal/login", json={"password": "guess"}).status_code
             for _ in range(app_module.LOGIN_RATE_LIMIT + 1)]
    assert codes[:-1] == [401] * app_module.LOGIN_RATE_LIMIT
    assert codes[-1] == 429


def test_changing_the_password_signs_everyone_out(api, monkeypatch):
    client, app_module, *_ = api
    headers = _login(client)
    assert client.get("/internal/session", headers=headers).status_code == 200
    monkeypatch.setattr(app_module, "INTERNAL_PASSWORD", "a-new-password")
    assert client.get("/internal/session", headers=headers).status_code == 401


def test_internal_routes_do_not_exist_without_a_password(api, monkeypatch):
    client, app_module, *_ = api
    monkeypatch.setattr(app_module, "INTERNAL_ENABLED", False)
    assert client.post("/internal/login", json={"password": "x"}).status_code == 404
    assert client.get("/internal/session").status_code == 404


# --- Live (optional) ------------------------------------------------------
@pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 (uses Voyage)")
def test_live_public_retrieval_never_returns_internal_text():
    from engine import RetrievalEngine
    eng = RetrievalEngine()
    try:
        for q in INTERNAL_QUESTIONS:
            for _pid, _score, source, text in eng.search(q):
                assert source not in internal.MANIFEST
                assert MARKER not in text
    finally:
        eng.close()
