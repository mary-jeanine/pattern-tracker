# Build plan

Each phase is sized to roughly one Claude Code session and ends with something
runnable. Work them in order — later phases assume earlier ones are stable.
Phase 2 is the risky one; everything else is ordinary CRUD and plumbing.

Status: **Phase 3 done** (Phases 0-2 complete too). SVG/PDF export, curve
flattening + polyline offset for the seam allowance, page tiling, and the
`GET /patterns/{id}/versions/{v}/pdf` endpoint are all in and tested — but
print scale is NOT yet physically verified (needs a printed page and a
ruler on the 50mm calibration square; see CLAUDE.md).

**2026-10-09:** `api/drafting/bodice.py` (Phase 2's output) was rebuilt
twice following the inthefolds.com bodice-block tutorial. First pass fixed
the armhole-bulge bug recorded in `docs/CASE_STUDY.md` §6 — direct
pivot/compass shoulder construction, two darts per piece,
`armscye_depth`/`shoulder_length` added to `Measurements`. Second pass,
prompted by the user's own measurements and reference images, re-fetched
the tutorial in full and implemented its actual seven-point armhole
construction instead of a single-bezier approximation; this surfaced and
fixed two more real bugs (a canceled-out clamp, and point A1 measured from
the wrong reference) that were only visible once rendered — not caught by
the test suite, consistent with this project's "render and look" rule.
`nape_to_bust` added to `Measurements`; `back_width` now read as a FULL
measurement, not pre-halved. Tested against the user's own real
measurements; visually re-checked via a rendered PDF; still not checked
against a muslin. See CASE_STUDY.md §6 for exactly what's faithful vs.
simplified vs. still flagged as an open judgment call.

Phase 4 (croquis interpretation) or Phase 5 (revision API) is next — PLAN.md
calls these independent once Phase 2 landed, so either order works.

---

## Phase 0 — Repo setup

**Goal:** the project runs and tests execute.

- `uv init`, add `fastapi`, `uvicorn`, `pytest`, `anthropic`, `svgwrite`,
  `reportlab`
- `.env` for `ANTHROPIC_API_KEY`, gitignored
- `api/db.py`: connection helper that executes `schema.DDL` on first run
- Confirm `tests/example_bodice.py` still runs from the repo root

**Done when:** `uv run pytest` passes with one trivial test and the example
script prints its diff.

---

## Phase 1 — Harden the schema

**Goal:** the data model is trustworthy before anything is built on it.

- Port dataclasses to Pydantic models (all at once — see CLAUDE.md)
- Add `infer_change_type(deltas) -> ChangeType` so revisions classify
  themselves: paths containing `.darts[` or dart leg points → `DART`,
  `seam_allowance_mm` → `SEAM_ALLOWANCE`, `ease_` → `EASE`, and so on
- Tests for `diff_patterns`: reordering a list produces no deltas; renaming a
  piece produces an add plus a remove; a nested bezier control point change is
  caught; an unchanged pattern diffs to an empty list

**Done when:** diff behaviour is covered by tests, including the reorder case.
That one matters most — it is the whole reason lists are keyed by id.

---

## Phase 2 — Drafting engine ⚠️ highest risk

**Goal:** `(Measurements, DesignIntent) -> list[PatternPiece]` for a basic
fitted bodice block, front and back.

**Decide first, before writing code:** which drafting system to follow. Aldrich
*Metric Pattern Cutting* is the usual reference and is already metric, which
matches the mm convention. Pick one and cite it in a module docstring. Do not
blend systems or work from half-remembered formulas.

- `api/drafting/bodice.py`, pure functions, no I/O
- Derive construction values from measurements and ease: back width, chest
  width, armhole depth, neck width and drop, shoulder slope, dart intake and
  placement
- Generate points in the order a human drafts them, naming each after the
  drafting step so revision paths stay readable
- Tests assert *relationships*, not magic numbers: the front armhole is longer
  than the back; total waist width equals waist measurement plus ease minus
  dart intakes; shoulder seam lengths match between front and back; the neckline
  is a smooth curve with no backtracking

**Done when:** a bodice generates from real measurements and the relationship
tests pass. Treat the output as a first draft, never as a fitted pattern.

**Risk:** this is where a model is most likely to produce confident nonsense.
Arithmetically valid geometry can still be unwearable. Compare generated values
against a published block for the same measurements before trusting it.

---

## Phase 3 — PDF export

**Goal:** a printable, correctly scaled, tiled PDF.

- `api/export/svg.py`: geometry → SVG. Offset the stitching line outward by
  `seam_allowance_mm` to produce the cut line; draw both. Render darts, notches,
  grainline, and a piece label with cut count and mirroring.
- `api/export/pdf.py`: tile the SVG across Letter or A4 pages with overlap
  margins, page-edge join markers, and page numbers
- **A 50mm calibration square on page 1 is mandatory.** Print at 100% scale and
  measure it with a ruler before trusting any pattern that comes out.
- Endpoint: `GET /patterns/{id}/versions/{v}/pdf`

**Done when:** a printed page's calibration square measures 50mm, and taped
tiles line up at the join markers.

**Note:** curve offsetting is genuinely fiddly. Offsetting a cubic bezier
exactly is not possible in closed form — flatten to a polyline, offset, then
refit, or just offset the flattened polyline and accept it.

---

## Phase 4 — Croquis interpretation

**Goal:** sketch upload → `DesignIntent`.

- `POST /croquis` accepts an image, stores it, returns a draft `DesignIntent`
- Vision call to the Anthropic API asking for **strict JSON matching the
  `DesignIntent` schema and nothing else** — no prose, no markdown fences
- Strip fences defensively anyway, validate against the schema, and on a parse
  failure return the raw text for manual correction rather than retrying blindly
- The returned intent is a **proposal you edit**, never applied automatically.
  Vision will misread seam lines and style details, and a wrong `garment_type`
  silently sends Phase 2 down the wrong drafting path.
- Don't re-collect body measurements on every croquis upload. Default a new
  pattern's `Measurements` from the most recent existing pattern (single user,
  these rarely change) and let the review screen confirm/adjust before
  creating version 1 — not a separate measurements endpoint. Measurement
  changes after that go through the normal `POST /patterns/{id}/revise` flow
  (Phase 5) like any other edit — `infer_change_type` already classifies
  `measurements.*` paths as `MEASUREMENT`, so this needs no new machinery.
- Creating the pattern from an approved intent writes version 1 with a
  `CROQUIS_DRAFT` revision whose reason records what the vision step concluded
- Before drafting (Phase 2) runs, check whether a reference sloper exists for
  this garment type (Phase 6). If one does, draft from its proven geometry
  instead of the generic formulas; if not, fall back to the formulas as
  today. Either way the result is still a first draft, not a fitted pattern.

**Done when:** a sketch yields a sensible editable intent, and approving it
produces a version 1 that exports to PDF.

---

## Phase 5 — Revision API

**Goal:** edits are recorded with your reasoning attached.

- `POST /patterns/{id}/revise` — takes a modified pattern plus `reason`,
  `fit_notes`, `source`; diffs against current, infers change type, writes a new
  snapshot and a revision row, bumps `current_version`
- `GET /patterns/{id}/revisions` — timeline, newest first
- `GET /patterns/{id}/diff?from=2&to=5` — aggregate deltas across versions
- Reject a revise call whose diff is empty, and one whose `reason` is blank —
  an unexplained revision is invisible to the whole RAG layer later
- `POST /patterns/{id}/versions/{v}/mark-sloper` — explicitly marks this
  version's bodice piece(s) as the reference block for this garment type.
  Deliberately a separate, human-initiated action, not something the system
  infers from "N clean revisions in a row" — whether a pattern fits is
  exactly the kind of judgment CLAUDE.md already says needs a human, not a
  model guessing from revision-count heuristics.

**Done when:** a round trip of load → edit → revise → read back shows the
correct deltas and reason, and marking a version as the reference sloper is
reflected in what Phase 4 drafts from next time.

---

## Phase 6 — Body profile, reference sloper, and the RAG layer

**Goal:** make later drafts better than earlier ones, specifically for this
body — not just a Q&A box over old revisions, though that's part of it.

**Why this is the core of the project, not a bolt-on:** an LLM drafting a
bodice has no embodied feedback and can be confidently, consistently wrong
(see CLAUDE.md's new "Why AI-generated drafting specifically needs this"
note — Phase 2's armhole passed every automated check while being thirty
times too deep). The fix isn't a better prompt each time; it's giving the
next draft real prior correction data for *this* body instead of starting
from the same generic formulas and the same blind spots every time.

**Body profile:**
- A persistent record, seeded from the full measurement set (the 42-point
  chart this project's measurements were scoped from — `Measurements`
  currently covers a curated subset; expand it as a real garment type
  actually needs a field, not all at once speculatively)
- Distinct from a single pattern's `Measurements` snapshot — the profile is
  "current best known measurements," a pattern version is "what was true
  when this version was drafted." Phase 4's existing behavior (default a new
  pattern's measurements from the most recent one, changes flow through
  `/revise`) already treats the most recent pattern as a de facto profile;
  this phase makes that explicit as its own entity rather than an implicit
  convention, so it stops drifting if patterns are deleted or a garment type
  with different relevant measurements is added.

**Reference sloper:**
- A pattern version explicitly marked via Phase 5's `mark-sloper` endpoint —
  never inferred automatically from revision count or any other heuristic
- When Phase 4 drafts a new bodice-derived garment and a sloper exists for
  that garment type, draft from the sloper's actual fitted geometry
  (`PatternPiece` points/edges/darts) rather than re-deriving from
  `api/drafting/bodice.py`'s formulas — adapted for new design features, not
  regenerated from scratch
- No sloper yet (first bodice, or a garment type with no block relationship
  to one) → formulas remain the fallback, exactly as Phase 2 built them

**RAG layer** (needs Phase 5):
- `api/rag/embed.py`: embed `Revision.embedding_text()`, store float32 BLOBs.
  **Embeddings are local hashed bag-of-words, not a semantic embeddings API**
  (Anthropic has no embeddings endpoint, and Voyage/OpenAI were considered and
  rejected — see CLAUDE.md's "Why local embeddings, not a semantic embeddings
  API" for the reasoning, not just the conclusion). Tokenize, drop a small
  stoplist of the revision-text template's own boilerplate words, hash each
  token into one of N buckets, L2-normalize — no vocabulary table, no training
  step, no new dependency or API key. `revision_embeddings.model` records
  which method produced a given vector, so this is swappable later without
  guessing what's already stored.
- Called automatically at the end of `POST /patterns/{id}/revise`, not only as
  a separate backfill step — a revision that isn't embedded the moment it's
  written is invisible to retrieval until someone remembers to run the
  backfill, which defeats the point of "ask about what just happened."
- `api/rag/retrieve.py`: cosine over the pattern's revisions. Filter by
  `pattern_id` **first**, then rank — cross-pattern bleed is the obvious failure
  here. Support a date-range filter for "what changed between March and June"
- `api/rag/answer.py`: retrieve, then pass the full revision records — deltas
  included — to the LLM. The prompt must instruct it to quote measurements only
  from the structured deltas, and to say so plainly when the notes do not
  explain a change
- `POST /patterns/{id}/ask`, returning the answer plus the revisions it used so
  you can check it
- Backfill embeddings for revisions written before this phase (anything from
  Phase 5 usage prior to Phase 6 landing)

**What "auto-apply" means here, and what it doesn't:** the body profile and
sloper feed a new draft's *starting point* automatically, without asking
first — no extra confirmation step before generation. What doesn't change:
the output is still never reported as correct, only generated. A smarter
starting point is not a fit guarantee; the muslin check CLAUDE.md already
requires stays required every time.

**Done when:** a second bodice-derived garment, drafted after a sloper is
marked, visibly starts from the sloper's geometry rather than the generic
formulas — and "why did I change the bust dart?" still returns the real
reason and cites the revision it came from.

---

## Phase 7 — Frontend

**Goal:** usable from the portfolio site.

- Entry from the sewing box / tin shelf item: popup blurb, then the full page
- Measurement form and croquis upload
- Intent review screen — editable, since the vision step is a proposal
- Version timeline with reason and fit notes per entry
- Pattern viewer rendering the SVG, with an overlay mode showing two versions in
  different colours
- Ask box, showing cited revisions alongside the answer
- PDF download per version

**Done when:** a pattern can be created, revised and queried without touching
the API directly.

---

## Phase 8 — Polish

- Backup: the SQLite file is the entire archive. Automate a copy somewhere.
- Export a pattern plus its full history as JSON, so the work outlives the app.
- Revision tags for muslin rounds, so "first fitting" groups several edits.

---

## Suggested session prompts

Phase 2: *"Read CLAUDE.md and docs/PLAN.md. Implement Phase 2 using Aldrich
Metric Pattern Cutting for the bodice block. Pure functions only. Write the
relationship tests first, then the drafting code."*

Phase 6: *"Read CLAUDE.md and docs/PLAN.md. Implement Phase 6 — the body
profile, the reference sloper, and the RAG layer. Filter by pattern_id
before ranking, and make the answer prompt read measurements from
structured deltas rather than from embedded text. The profile and sloper
feed Phase 4's drafting step automatically, without an extra confirmation
step, but the output is still never reported as correct, only generated."*

## Ordering notes

Phases 3, 4 and 5 are independent once Phase 2 lands, so take them in whatever
order is appealing. Phase 7 needs whatever it displays.

Phase 6's RAG layer needs Phase 5 (it embeds revisions). The body
profile/sloper half of Phase 6 and Phase 4 reference each other (Phase 4
checks for a sloper before drafting; the sloper only has something to mark
once Phase 4 and Phase 5 exist) but aren't a hard sequencing dependency —
build Phase 4 first with the sloper-check always falling back to the Phase 2
formulas, then Phase 6 makes that check real.

Build Phase 5 before accumulating real pattern work — revisions made before
logging exists are lost, and the history is the point of the project.
