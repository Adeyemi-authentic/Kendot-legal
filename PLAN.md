# PLAN: Law Firm Website + Built-in RAG Assistant (practice build)

Status: **Phase 7 internal assistant BUILT locally (2026-10-06), awaiting your review.** Phases 0-6 done (live on Vercel + Render + Neon). Separation proven by `api/tests/test_separation.py` (13 tests). Not yet deployed: Neon internal table, Render INTERNAL_PASSWORD. Firm name: Kendot Legal.
Created: 2026-09-30

## 1. Purpose

Practise the exact package we will give a real firm for free (and later sell for £3,000), using a **fictional
concept firm**, before a client says yes. The practice build must produce three things:

1. **A deployed demo** you can show prospects: a law firm website with a working assistant.
2. **A repeatable process.** Everything is set up so a real firm's version is mostly content swaps.
3. **SKILL.md** (written after the build succeeds): the playbook for building each client's free version.

It is labelled "concept build: fictional firm" everywhere, as the 5-month plan (Section 2.5A) requires.

## 2. What we are building

**Fictional firm (placeholder name): "Kendot Legal"**, a mid-sized Lagos/Abuja firm covering corporate,
real estate and tenancy, tax, IP and data protection, and disputes. The name can be changed. It should be
checked so it doesn't match a real Nigerian firm.

| Part | What it is |
|---|---|
| **Website** | Home, About, Practice Areas (5 pages), People (4 to 6 profiles), Insights (8 to 10 articles), Contact, Privacy Notice. Mobile-first and fast. |
| **Public assistant** | A chat widget on every page. It answers only from the site's own content, links every citation to the page it came from, gives no legal advice (NBA 2024 AI guidelines), and says "I don't know" when the answer isn't in the content. |
| **Lawyer handoff / intake** | When a visitor needs a lawyer, the assistant offers a short enquiry form (name, contact, matter type, brief description, NDPA consent checkbox). The form is saved to the database and includes a WhatsApp link. |
| **Internal assistant** (stretch, Phase 7) | A password-protected /internal page over separate "precedent" documents that are never visible to the public assistant. This shows we can keep the two apart. |

## 3. Architecture

```
  [ Astro + Tailwind static site ]  --deployed to-->  Vercel (free)
     src/content/  (practice areas, insights, FAQs, people as Markdown)
        |                                   chat widget (JS island)
        |  sync script                          |  fetch + stream (SSE)
        v                                       v
  [ api/rag/docs/ ]  --ingest-->  [ FastAPI RAG service (reused from naija-law-rag) ] --> Render
                                        |  Voyage embed + rerank, Claude Haiku + citations
                                        v
                                  [ Neon Postgres + pgvector ]  (chunks + intake leads)
```

Key design decisions:
- **The website's Markdown content is the assistant's knowledge base.** A new insight is published once and
  the assistant knows it after re-indexing. This is what makes the build repeatable per client: swap the
  content, re-index, done.
- **Citations link to real pages.** Each chunk stores its page's URL, so a source badge opens the exact
  insight or practice page. That is more convincing for lawyers than a file name.
- **Reuse the backend.** The body and brain (hybrid retrieval, rerank, gate, citations, coverage, SSE,
  pgvector) come from `naija-law-rag` and are covered in `MLRevive/SKILL.md`. Only the new parts get built from scratch.

**New work compared with naija-law-rag:**
1. **Security for a public widget.** The site is public, so an API key in the browser would be visible to anyone. Protection instead comes from: a CORS allowlist (only the firm's
   domain), a per-IP rate limit, a question length cap, a daily spending cap on AI usage, and no API-key auth on
   `/chat`. (Admin routes keep the key.)
2. **Stopping legal advice.** New system prompt rules and a new eval category: "Should I sue my landlord?"
   must get a polite handoff to a lawyer, not advice.
3. **Intake endpoint.** `POST /intake` with Pydantic validation and a consent flag, stored in a `leads` table.
4. **Content sync.** A script that converts the Astro Markdown into the RAG corpus, with URL metadata.
5. **Cold starts.** Render's free tier sleeps. The widget pings `/health` when the page loads, so the backend is
   warm by the time someone types. For a real client the fix is the paid tier (~$7/mo, included in the support plan).

## 4. Folder layout (planned)

```
lawfirm-site-rag/
  PLAN.md             this file
  README.md           case study: problem, build, screenshots, eval numbers, live URLs
  web/                Astro + Tailwind site (content in web/src/content/)
  api/                FastAPI RAG service copied from naija-law-rag, then adapted
    rag/docs/         generated from web/src/content by scripts/sync_content.py
  scripts/            sync_content.py, validate_golden.py
  eval/               golden.jsonl, success criteria, baseline results
```

## 5. Phases (each ends with a check you can see)

| # | Phase | Output | Done when |
|---|---|---|---|
| 0 | **Setup** | Folder, git repo, `api/` copied from naija-law-rag, Astro scaffolded | Both run locally ("hello" page + `/health`) |
| 1 | **Content** | Firm profile, 5 practice pages, 4 to 6 people, 8 to 10 insights, FAQs, privacy notice | Content reviewed by you. Insights are based on the statutes we already have (Lagos Tenancy Law, Tax Admin Act 2025, Constitution) plus NDPA and CAC basics, and are written as general information |
| 2 | **Website** | All pages built, responsive, fast | Looks professional on a phone and a laptop. Lighthouse performance and accessibility scores of 90+ |
| 3 | **Backend changes** | Public-mode security, legal-advice rules, `/intake`, URL-aware citations, content sync, re-indexing | Local end-to-end: question, then cited answer with page links. Refusal works. An advice request gets a handoff |
| 4 | **Chat widget** | Streaming chat bubble, source badges linking to pages, handoff form, disclaimer, warm-up ping | Works on every page. Usable on mobile |
| 5 | **Evaluation** (write targets BEFORE running) | 30 to 40 question golden set: answerable, hard negatives, paraphrases, **advice-seeking**, a few prompt-injection attempts. Validated by script | Targets met or failures explained. Baseline saved |
| 6 | **Deploy** | Web on Vercel, API on Render, Neon index built | Public URLs live, widget works from the live site, `/health` green |
| 7 | **Stretch: internal assistant** | Password-protected /internal page over separate precedent docs | Public assistant provably cannot retrieve internal docs (a test proves it) |
| 8 | **Package** | README case study, 60-second Loom, **SKILL.md** for client builds | You can follow SKILL.md to rebuild for Stren & Blan |

**Eval targets** (confirmed in `eval/TARGETS.md` before measuring, plus a referral target):
- Answer correctness 90% or higher on answerable questions
- Citation accuracy 100%
- Out-of-scope refusal 100%, with false refusals no more than 10%
- Advice-seeking questions handed off (not answered) 100%
- Prompt-injection attempts resisted 100%
- Median time to first streamed token under 3s when warm; cost per question under $0.005

## 5A. Phase 7 notes (internal assistant)

- **Separate store, separate engine object.** `api/rag/internal_docs/` (5 fictional precedents and procedures, each
  marked `KL/INT/`) is indexed into its own Qdrant folder (`rag/index_internal/`) or pgvector table
  (`INTERNAL_TABLE`). The public routes only hold `app.state.engine`; nothing in a public request selects a store.
  `internal.make_engine()` refuses to open the public store.
- **Door.** `POST /internal/login` (password from `INTERNAL_PASSWORD`, 5 tries per 15 min per IP) returns a signed,
  stateless token valid 8 hours. Changing the password signs everyone out. No password set = routes return 404.
- **Proof.** `api/tests/test_separation.py`: corpus (no marker in public docs), stores (no internal chunk in the
  public index), routes (public chat only ever searches the public store, even with a staff token; internal routes
  reject missing, forged and expired tokens). `RUN_LIVE=1` adds real retrieval over the public index.
- **Image.** `.dockerignore` drops `internal_docs/*.md`; production reads internal text from Neon only.
- **For a real client:** never commit their internal documents to git. Keep them outside the repo and build the
  table from a local folder. Treat the staff password like any shared credential (rotate when staff leave).

## 6. Stack and costs

- Web: Astro 5 + Tailwind, deployed on Vercel free tier (Node 24 is installed locally).
- API: Python 3.14, FastAPI, Anthropic `claude-haiku-4-5` with native citations, Voyage `voyage-3.5-lite` + `rerank-2-lite`,
  pgvector on Neon (free), Render (free for practice).
- Expected practice cost: under $2 in API usage for the whole build and evaluation.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Invented legal content could be wrong | Insights are general information that cite the statutes we hold and carry a "not legal advice" notice. You review Phase 1 content |
| Fictional name matches a real firm | Check the name before publishing, and keep the "concept build" label visible |
| Public endpoint abuse (cost) | Rate limit, length cap, daily spending cap, CORS |
| Cold start makes the demo feel broken | Warm-up ping. Tell prospects the paid tier removes it |
| Scope creep (CMS, logins, analytics) | Out of scope for practice. List them as paid add-ons in SKILL.md |

## 8. Decisions needed before we start (defaults shown)

1. **Working mode:** *default: I build, you review at the end of each phase.* The alternative is tutor mode (you type, I guide), which is slower but gives you more hands-on practice.
2. **Firm name:** *default: "Kendot Legal"*. Suggest another if you prefer.
3. **Internal assistant (Phase 7):** *default: include it.* It's what sells the £3k tier.
4. **Web hosting:** *default: Vercel.* Netlify works just as well.

Reply "go" (and change any defaults) to start Phase 0.
