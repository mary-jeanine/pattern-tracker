# Case study — working notes

This is the **ongoing draft** for the portfolio case-study page described in
the deployment decision below — not the final page copy. It gets updated as
each phase lands, per its own section's status. Where a section reports a
number or a test result, that's real, pulled from an actual run of the
suite — not a projection of what a phase will eventually show.

**Deployment decision (2026-10-08):** this tool runs locally only. No live
deployment. See `CLAUDE.md`'s intro for the reasoning (API key exposure,
personal body-measurement data, a live demo having nothing to show a
visitor without their own croquis and muslin, and the revision history
only being real if it comes from actual sewing). The portfolio's sewing
box / tin shelf item links to this case study, not a running feature. The
repo is intended to be public; sample measurements only, real measurements
stay in the gitignored `.db` file.

Status at last update: **Phases 0-3 complete** (project setup, schema +
diff/change-type, bodice drafting, SVG/PDF export). Phases 4-8 not started.

---

## 1. Problem and scope

**Status: done, stable, but reframed 2026-10-09 — the version before this
undersold the actual problem. See the note at the end of this section.**

AI-generated sewing patterns have real, specific limitations, not generic
"AI can be wrong" ones:

- **No embodied feedback.** A model drafting a bodice has never worn a
  garment. It cannot know a dart pulls or an armhole binds without a human
  telling it, after a physical fitting — there is no other way that
  information enters the system.
- **Confident, internally-consistent, wrong geometry.** This project's own
  Phase 2 armhole curve passed every automated relationship check (front
  longer than back, areas, closure) while its control-point bulge was
  roughly thirty times larger than a real armhole's deviation. The checks
  were satisfied; the shape was still unwearable. A model can be
  self-consistent and wrong in a way no check *it was given* catches.
- **No memory between generations, by default.** Every drafting call is
  stateless unless something deliberately feeds prior context back in —
  so the same correction gets made, and the same class of error gets
  re-committed, every single time a new pattern is drafted from scratch.

Pattern Tracker's actual point is closing that third gap specifically: a
structured body-fit profile and a **reference sloper** — a bodice version
explicitly marked, once it's been fitted, as the trusted block — so later
bodice-derived drafts start from *proven* geometry for *this body* instead
of generic formulas re-run from zero. That's real patternmaking practice
("draft from a block," not from scratch each time), not a bigger prompt.
The revision history underneath it (every edit as a structured diff plus
the reason) is what makes a sloper trustworthy in the first place and what
the profile gets built from — necessary infrastructure, not the point on
its own.

It's scoped to personal use, single user, single body: no accounts, no
sharing, no multi-tenant anything. That scope is what makes several later
design calls — SQLite over Postgres, brute-force cosine over a vector
database, local-only over deployed — the right call rather than a
shortcut. At this scale (low hundreds of revisions, one user) the simpler
option is also the more correct one.

**Note on the reframe:** the original version of this section described
Pattern Tracker as version control for patterns — true, but framed as the
point rather than as plumbing underneath the actual point (making later
AI-generated drafts better for this specific body). Caught by the user
reading the published build log and noticing the write-up didn't match
what they'd actually asked to build — not by any test, which is exactly
the kind of gap tests don't catch.

## 2. System design

**Status: done** (architecture is stable; diagram below reflects the
local-only decision)

Two pipelines share one data model:

1. **Draft** — croquis sketch + body measurements → a structured pattern
   (`Measurements`, `DesignIntent` → `list[PatternPiece]`), exportable to PDF.
2. **Track** — every later edit is diffed against the previous version,
   classified by what kind of change it is, and logged with your reasoning
   in your own words.
3. **Learn** *(designed, not built — Phase 6)* — a body-fit profile and an
   explicitly-marked reference sloper feed back into step 1, so a later
   bodice-derived draft starts from proven geometry for this body instead
   of the generic formulas every time.

The tracker isn't a separate system bolted onto the drafting tool — it's
simply "version 1 onward" of whatever the draft step produced, and the
profile/sloper loop is simply what versions 2-onward eventually feed back
into version 1 of the next pattern.

**Named-points schema.** Geometry is never a raw SVG path string. Every
point has a stable, permanent string id (`bust_point`, `shoulder_tip`), and
edges/darts/notches/grainlines reference those ids. This is what makes a
diff meaningful: comparing two path strings tells you nothing, but
comparing two point positions by id tells you exactly what moved. Lists
(points, pieces, segments) are keyed by id when diffed, so **reordering a
list produces no deltas** — this was the first thing tested in Phase 1,
because it's the whole reason lists are keyed by id in the first place.

**Why snapshots and diffs are stored separately from the embedded
reasoning text.** `pattern_versions` holds a full snapshot per version —
any version re-exports to PDF without replaying history. `revisions` holds
the structured deltas (`FieldDelta.path/old/new`) separately. Only
`Revision.embedding_text()` — a natural-language blob built *from* those
deltas, for the future RAG layer to search — gets embedded. The RAG layer
is never the source of a measurement it reports; a question like "what did
I change the bust dart to?" retrieves the revision, then reads the actual
number from `deltas`, never from what the embedded text happened to say.
This separation exists specifically so retrieval can't silently paraphrase
a number wrong.

## 3. Implementation details

**Status: done** (stack decisions for Phases 0-3; later phases may add to this)

**Stack:** Python 3.11+, FastAPI, SQLite (`uv`-managed, `hatchling` build
backend for a flat `api/` package layout). Pydantic for the schema
(`pydantic.dataclasses.dataclass`, not `BaseModel` — chosen specifically to
keep positional construction working everywhere it was already used,
rather than rewriting every call site). `reportlab` for PDF, `svgwrite`
for SVG. React/TypeScript, reused from the portfolio site's own components,
for whatever frontend eventually exists (not built yet — see §5).

**Invariants (from `CLAUDE.md`), carried through every phase so far:**
- All lengths are millimetres, everywhere, as floats.
- Geometry is named points, never raw path strings.
- Ids are stable and permanent — the diff keys lists by id.
- The stitching line is stored; seam allowance is one float per piece,
  offset at export time, not baked into stored coordinates.
- Every version is a full snapshot; revisions are derived, not authoritative.
- Only `embedding_text()` is embedded; the RAG layer never originates a number.

**Skipping a vector database.** Revision embeddings are stored as float32
BLOBs in SQLite and searched by brute-force cosine in Python. At this scale
— one user, low hundreds of revisions — a linear scan over a few hundred
rows is faster than the import cost of a vector-DB client library, and it
avoids pgvector/Chroma/Qdrant/FAISS as dependencies entirely. This isn't
built yet (Phase 6), but the decision was made up front specifically so
Phase 6 doesn't second-guess it under time pressure.

## 4. Test outcomes

**Status: Phases 0-3's results are real and current as of this update.
Phase 3's physical print-scale check is still outstanding.**

Current suite: **63 tests passing**, spanning:

| Area | Tests | What they check |
|---|---|---|
| `diff_patterns` | 4 | Reordering → no deltas, renaming → add+remove, a nested bezier control-point change is caught precisely, unchanged → empty diff |
| `infer_change_type` | 12 | Priority ordering (a dart move that also nudges seam allowance reads as DART, not SEAM_ALLOWANCE), piece add/remove, width-vs-length only fires when point deltas agree on an axis |
| DB bootstrap | 1 | Schema creates on first connection |
| Bodice drafting relationships | 7 | Front armhole longer than back, waist-width identity holds exactly, shoulder seams match, necklines don't backtrack, back wider than chest (bust-derived fallback only), armhole depth scales with bust, supplied measurements override the guessed fallback |
| Export geometry | 10 | Outline reconstruction is closed and visits every point, seam-allowance offset actually expands the shape (not shrinks it), polygon centroid/point-in-polygon correctness including the exact bbox-corner-outside-the-piece case that broke the label fix once already |
| SVG export | 5 | Physical mm units declared, both stitching and cut lines present (plus grainline arrowheads), one line pair per dart, label includes cut count/mirroring/seam allowance, label is actually inside the polygon (point-in-polygon, not a bbox check) |
| PDF export | 21 | Valid PDF bytes, page count matches independently-computed tiling math, all three calibration squares (50mm/1in/1cm) appear exactly once, every tile carries a lettered/numbered code with trim-line captions pointing at its neighbour, label is actually inside the polygon, label text that would overflow the clip gets clamped on both axes to fit, adjacent tiles sharing a zero-overlap boundary compute the exact same seam coordinate, a match-mark triangle is placed only where an actual piece crosses that seam, complementary half-diamond triangles share their flat edge and reconstruct a full diamond's four vertices, both Letter and A4 render |
| Live endpoint | 3 | `GET /patterns/{id}/versions/{v}/pdf` against a real seeded SQLite DB (not mocked) returns a PDF or the right 404 |

**Six real problems were caught across this phase — two by tests, three by
physically printing or visually rendering the output, and one by a test
that passed while checking the wrong property:**
1. **Phase 1:** `Segment` had no stable id, so a bezier control-point change
   fell back to a whole-list comparison instead of a precise delta. The
   test written against *unmodified* code caught this; the fix was to give
   `Segment` a derived id (`f"{start}->{end}"`).
2. **Phase 3, export geometry:** the first seam-allowance offset
   implementation shrank the piece inward instead of expanding it outward —
   caught immediately by a test asserting the cut line's area must exceed
   the stitching line's.
3. **Phase 2, the armhole curve — not caught by any test.** The bodice was
   printed, taped together, and the armhole curve ballooned into a huge
   rounded lobe instead of a modest scoop. The control-point bulge turned
   out to be 81.7mm on the front piece — over thirty times larger than a
   real armhole's typical 1.5-2.5cm deviation — because the bulge fraction
   had been tuned only to satisfy a relationship test (front armhole longer
   than back), with nothing underneath it resembling a real armhole's
   shape. No automated test caught this because every test checked
   *relationships* (areas, lengths, closure), and the relationships all
   still held while the shape itself became absurd. A follow-up comparison
   against a published bodice-block tutorial confirmed the method itself
   needs rebuilding, not just this one constant — see the updated §6.
4. **Phase 3, the piece label — fixed wrong once, then fixed again.** The
   first version placed the label relative to the piece's raw local origin,
   which could sit outside the piece once the seam allowance was added.
   The first *fix* moved it to the stitching outline's bounding-box corner
   plus a small inset, with a test confirming the point fell within the
   bbox's x/y range — but a bounding-box range isn't the same claim as "is
   this point on the fabric." The bodice front's neckline curve is concave
   near that exact corner, so the "fixed" label still printed in the empty
   space above the neckline, outside the piece entirely — caught by
   reprinting and looking, not by the test, because the test checked a
   weaker property than the one that mattered. The real fix uses the
   polygon's area-weighted centroid, verified with an actual ray-casting
   point-in-polygon test (`api/export/geometry.py`), and that same check
   now guards both exporters' labels going forward.
5. **Phase 3, the trim line vs. the printable clip — a chain of three.**
   Once tile seams were moved to the exact overlap midpoint (for the
   half-diamond match marks), the printable clip stayed at the full page
   margin — so pattern lines kept printing past the point the trim line
   told you to cut. The user caught this from the rendered PDF, not from
   any test. Fixing it (clip to the seam boundary instead) immediately
   surfaced a second bug in my own verification render: the tighter clip
   now truncated piece labels that used to have more headroom. Fixing
   *that* by clamping the label position to the measured text width
   surfaced a third: the clamp had no check that the label's un-clamped
   anchor was even on this page, so a label belonging on a different tile
   got dragged into view and overlapped the correct one. All three were
   caught by actually rendering pages to images and looking — reading the
   code after each change would have missed every one of them.
6. **Phase 3, the margin doubled.** Asked to make the margin exactly 0.5in
   on every edge, the first fix added `margin_mm` as a canvas-space inset
   on top of `to_pt()`'s own coordinate mapping — which *already* applies
   `margin_mm` when translating canvas space to the page. The result was a
   uniform margin, just twice the requested size (25.4mm, not 12.7mm); every
   test still passed, because the tests checked internal consistency
   (all four edges agree with each other), not the correct absolute value.
   Caught by directly measuring the rendered PDF's drawn coordinates, not
   by inspection. Fixing it surfaced a real design conflict: an *exactly*
   uniform margin forces each page to clip at its own full window, which
   means two adjacent pages' trim lines are no longer one shared
   coordinate — breaking the half-diamond-splits-at-the-cut premise from
   finding 4. The actual fix was to tile with **zero overlap** between
   pages (each page's window starts exactly where the previous one ends),
   which makes the uniform margin and the exact shared cut line both true
   at once — confirmed by rendering adjacent tiles and checking the two
   triangle halves land on the identical coordinate.

**Outstanding:** the mandatory 50mm calibration square (plus 1in and 1cm
companions, added after the armhole finding above prompted a wider review
of the PDF's print-shop conventions) has been generated and a sample PDF
sent for printing, but **the physical ruler check hasn't been reported
back yet.** Per `CLAUDE.md`, nothing about print scale is trustworthy
until that happens — this section will report the actual measured value
once it exists, not before.

## 5. Static sample walkthrough

**Status: not started.** Needs a pre-recorded pattern with three
revisions, shown as a timeline with example Q&A — fixed data baked into
the page, no backend or API key involved. This is the next thing to build
once there's real revision data to draw from (naturally available once
Phase 5's revision API exists and has been exercised at least once).

## 6. What it can't do

**Status: accurate as of Phase 3; will grow as later phases surface more limits.**

- **Drafting is still only a first draft — but it's now a faithful
  transcription of a real construction method, not an approximation of
  one, and two more real bugs were found and fixed by rendering and
  looking, not by tests alone.** `api/drafting/bodice.py` has been
  rebuilt twice against the inthefolds.com bodice-block tutorial. The
  first rebuild (above) fixed the armhole-scale bug but simplified the
  curve to a single generic bezier and guessed at a couple of ambiguous
  steps. The second rebuild, prompted by the user supplying their own ten
  measurements and two reference images (a photographed fitted bodice and
  a tailor's curve/French-curve ruler), re-fetched the tutorial in full
  detail and implemented its actual seven-point armhole path (steps
  33-41: O-Z-B1-A3 on the back, A3-B2-A2-V on the front) instead of
  approximating it, smoothed with a Catmull-Rom spline through all four
  points per side rather than a single 2-control-point bezier. Two real,
  user-visible bugs turned up only once this was rendered and inspected
  against the reference curve shape: (1) a bezier-control-point math
  error where clamping a projected position algebraically canceled itself
  out and silently did nothing; (2) a misread of step 36 (point A1) that
  pinned it to the bust point's raw x-position instead of measuring from
  the back-width point as the source actually says, which pulled the
  front armhole's guide points almost to centre front and produced a
  pinched, self-crossing curve. Neither was caught by a passing test
  suite — both were only visible in a rendered page, which is exactly why
  this project's rule is to render and look, not just trust green tests,
  after any geometry change. Also corrected in this pass: `back_width` is
  read as a FULL across-back measurement and halved (the first rebuild
  treated a supplied value as already-halved), the waist-dart-width
  calculation uses raw `bust`/`waist` rather than ease-adjusted values
  (matching the source's own formula, which changes the waist-width
  identity — see `test_total_waist_width_equals_waist_plus_bust_and_waist_ease`),
  and `nape_to_bust` was added to `Measurements` as the real input the
  bustline height is drawn from. `Measurements.bust_height` (shoulder to
  bust) is captured per the tutorial's own measurement list but isn't
  consumed by any construction step found in the fetched source text —
  flagged as unused rather than silently dropped. Tested against the
  user's own real measurements (34in bust / 31.5in waist / etc., not a
  guessed "standard size"), and the rendered PDF now shows the tight,
  concave "hook" curve near the underarm that real armholes — and the
  tailor's curve rulers used to draw them by hand — actually have, not a
  uniform outward bulge. Still not checked against a muslin.
- **The outline reconstruction assumes one simple closed loop** per piece
  (every point has exactly two connections). True for everything the
  bodice drafter currently generates; a piece with branching geometry
  (an internal seam, a dart that isn't edge-attached) isn't handled.
- **Darts are drawn as surface annotations, not cut into the outline.**
  Real sewing-pattern convention varies between a cut V-notch and a
  fold-and-stitch dart; the simpler annotation-only interpretation was
  chosen without strong confidence it's the more common convention.
- **Print scale is unverified** until the ruler check lands (see §4).
- **No garment type beyond a bodice block yet.** Skirt/trouser/sleeve
  measurements already exist on `Measurements` (added ahead of need,
  deliberately) but no drafting logic uses them yet.
