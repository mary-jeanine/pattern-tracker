# Pattern Tracker

A sewing-pattern drafting and revision-tracking tool. Personal use, single
user, **runs locally only — never deployed live.** The portfolio site's
sewing box / tin shelf item links to a case study about this project
(`docs/CASE_STUDY.md`), not an embedded running feature.

**Why local-only, not a live deployment:** a public deployment means an
Anthropic API key and a vision/RAG endpoint sitting on a server anyone
could hit, needing rate limiting, auth, and spend caps just to be safe —
and it would store your own body measurements and fit notes on a public
host. It also wouldn't demonstrate anything: a visitor has no croquis and
no muslin, so a live version would just be an empty tool. The revision
history that's the actual point of this project only accumulates through
real sewing, which only happens on your machine. The repo itself can still
be public — code and `PLAN.md`/`CLAUDE.md` are readable, sample
measurements only, real measurements stay in the gitignored `.db` file.

## What it does

1. **Draft.** Croquis sketch + body measurements go in. A structured pattern
   comes out, exportable as a printable PDF.
2. **Track.** Every later edit to that pattern is logged as a structured diff
   plus the reason in your own words.
3. **Learn.** Once a bodice has been fitted and explicitly marked as your
   reference sloper, later bodice-derived drafts start from *its* proven
   geometry instead of generic formulas — real patternmaking's "draft from a
   block" technique, not just a bigger prompt. A structured body-fit profile,
   seeded from your full measurements and refined from revision history,
   feeds in alongside it. The pattern is still never called correct —
   starting from a better block doesn't skip the muslin.
4. **Ask.** A RAG layer over revision history answers questions like "why did
   I move the bust dart last time?" or "what changed between v2 and v4?"

**Why AI-generated drafting specifically needs this.** An LLM producing a
bodice draft has no embodied feedback — it has never worn a garment, so it
can't know a dart pulls or an armhole binds without a human telling it after
a physical fitting. It can also be confidently, internally-consistently
wrong: Phase 2's first armhole curve passed every automated relationship
check while being thirty times too deep, because nothing it was checked
against encoded what a real armhole looks like. Phases 3 and 6 are the
mitigation, in two different ways: Phase 3's calibration square is a
cross-check the model can't fake (a ruler either agrees or it doesn't), and
Phase 6's block/profile feedback loop gives it real prior correction data
for *this body* — specific, not a generic formula averaged from nowhere —
instead of starting from scratch and re-committing the same class of error
each time.

The tracker is not a separate system from the drafting tool. It is simply
"version 1 onward" of whatever the croquis step produced.

## Stack

- **Backend:** Python 3.11+, FastAPI, SQLite. No Postgres, no Docker, no queue.
  Personal scale means low hundreds of revisions, not millions.
- **Frontend:** React + TypeScript, inside the existing portfolio site. Reuse
  that repo's components and build setup rather than adding a second app shell.
- **Vector search:** store float32 vectors as SQLite BLOBs, brute-force cosine
  in Python. Do NOT add pgvector, Chroma, Qdrant, or FAISS. At this scale a
  linear scan over a few hundred rows is faster than the client library's import.
- **Embeddings:** local, hashed bag-of-words (the "hashing trick" — tokenize,
  drop a small stoplist of the revision-text template's own boilerplate, hash
  each token into one of N buckets, L2-normalize). No vocabulary table, no
  training step, no embeddings API or key. See "Why local embeddings, not a
  semantic embeddings API" below before swapping this for one.
- **LLM calls:** Anthropic API for croquis vision and RAG *synthesis* only —
  turning retrieved revisions into an answer. Retrieval itself (the embedding
  and the cosine search) makes no LLM or API call at all.

**Why local embeddings, not a semantic embeddings API (Voyage/OpenAI):**
Anthropic's API has no embeddings endpoint of its own, so Phase 6 needed a
real decision here, not a default. The first instinct was Voyage AI —
Anthropic's own recommended/partnered provider, and "real" semantic
embeddings are the standard choice for RAG generally. That instinct turned
out to be a generic best-practice default, not one actually reasoned from
this project's own constraints above: local-only, never deployed, personal
scale, and an explicit refusal to add vector-search infrastructure
(pgvector/Chroma/Qdrant/FAISS) for a few hundred rows. Voyage adds exactly
what that stance is wary of — a second API key, a second paid provider, a
network call fired on every single revision. What it buys in exchange is
retrieval quality that mostly matters when a query paraphrases away from the
stored vocabulary entirely. `Revision.embedding_text()` isn't prose, it's
templated — `change_type`, dotted point/dart paths, raw delta numbers, then
your own `reason`/`fit_notes` — and the one person who'll ever query it is
asking about their own patterns in the same sewing vocabulary the deltas
already use ("why did I widen the back waist dart" shares real tokens with
exactly the revision that answers it). Hashed bag-of-words is a good match
for that specific corpus and a poor one for open-domain paraphrase — a real
gap, but a narrower one here than it looks in the abstract. If retrieval
quality turns out to matter more in practice than this reasoning predicts,
`revision_embeddings.model` already records which embedding method produced
each stored vector, so a later swap doesn't require guessing what's stored.

## Repo layout

```
api/
  schema.py        # data model + structural diff + DDL. The linchpin.
  drafting/        # measurements -> geometry (the real pattern math)
  export/          # geometry -> SVG -> tiled printable PDF
  rag/             # embedding, retrieval, answer synthesis
  main.py          # FastAPI app
tests/
  example_bodice.py  # worked example: build v1, apply fit edit, diff
docs/
  PLAN.md          # phased build plan. Read this to pick up work.
```

## Invariants — do not violate these

These hold the two pipelines together. Breaking one silently corrupts the
revision history.

- **All lengths are millimetres,** everywhere, as floats. Convert to inches or
  centimetres only at the UI and PDF edges. No mixed units in stored data.
- **Geometry is named points.** Edges, darts, notches and grainlines reference
  point ids (`bust_point`, `shoulder_tip`). Never store raw SVG path strings as
  the source of truth — a diff between two path strings is meaningless.
- **Ids are stable and permanent.** The diff keys lists by `id`. Renaming a
  point id orphans every historical revision that referenced it. If something
  must be renamed, write a migration that rewrites stored revision deltas too.
- **Store the stitching line, not the cut line.** Seam allowance is one float
  per piece, offset at export time. This keeps "I changed the seam allowance"
  a one-field revision instead of a change to every coordinate.
- **Every version is a full snapshot** in `pattern_versions`. Revisions are
  derived, not authoritative. Any version must be re-exportable to PDF without
  replaying history.
- **Only `Revision.embedding_text()` is embedded.** Numbers live in `deltas`
  and are read directly. Never let the RAG layer be the source of a measurement
  it reports — retrieve the revision, then read its actual delta values.

## Commands

```bash
uv run uvicorn api.main:app --reload     # dev server
uv run pytest                            # tests
uv run python tests/example_bodice.py    # worked example, prints a diff
```

## Conventions

- Dataclasses are currently stdlib. Pydantic is fine to adopt — FastAPI takes
  either — but migrate the whole schema at once, not file by file.
- Drafting functions are pure: `(Measurements, DesignIntent) -> list[Point]`.
  No I/O, no database, no API calls. This is what makes them testable.
- **After any geometry change, render and look — at the FULL cut-on-fold
  piece, not just the half.** Every piece this project drafts is a half,
  mirrored across centre front/back for the real fabric. A half-piece view
  can look completely correct while hiding a real defect: a curve meeting
  the fold line with anything but a horizontal tangent there mirrors into
  a visible cusp, invisible until you actually join both halves. (Found
  this exact bug on the bodice neckline — correct-looking as a half,
  a sharp double-bump once mirrored.) Use
  `api.export.geometry.mirrored_outline(piece)` for this — it raises if a
  piece doesn't have exactly two points on the fold line, and its own
  docstring has the tangent-math reasoning. Passing tests aren't a
  substitute: `piece_outline()`/`offset_polyline()` tests only ever see
  one half, so they can't catch a fold-line discontinuity either — write
  a test against `mirrored_outline()`'s output when checking for one.
- New `ChangeType` and `EdgeRole` values are additive only. Stored revisions
  carry old enum values forever.

## Things that need a human, not a model

Flag these rather than guessing:

- **Whether a pattern actually fits.** Drafting math can be arithmetically
  correct and still produce a garment that pulls across the back. Only a muslin
  on a body tells you. Never report a draft as "correct", only as "generated".
- **Print scale.** A tiled PDF is wrong until a physical test square measures
  right with a ruler. Always include a 50mm calibration square on page 1.
- **Drafting formulas themselves.** Blocks vary by system (Aldrich, Armstrong,
  Müller). Ask which system to follow instead of averaging remembered numbers.
