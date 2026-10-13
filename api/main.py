"""FastAPI app. Currently exposes the Phase 3 PDF export endpoint, all of
Phase 5's revision API (revise, revisions, diff, mark-sloper), the Phase
4 lookup that reads mark-sloper's data, a sketch of Phase 4's own croquis
intake, and Phase 6's /ask endpoint over the RAG layer.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from anthropic import Anthropic
from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass as pydantic_dataclass

from api.db import get_connection
from api.export.pdf import render_pattern_pdf
from api.rag.answer import answer_question
from api.rag.embed import embed_and_store_revision
from api.schema import (
    DesignIntent, FieldDelta, GarmentType, Pattern, Revision, RevisionSource,
    diff_patterns, infer_change_type, pattern_from_json, revision_from_row,
)

app = FastAPI(title="Pattern Tracker")

CROQUIS_DIR = Path(__file__).resolve().parent.parent / "croquis"
_VISION_MODEL = "claude-sonnet-5"  # swap freely; nothing else depends on the exact id
_design_intent_adapter = TypeAdapter(DesignIntent)
_revision_adapter = TypeAdapter(Revision)
_field_delta_list_adapter = TypeAdapter(list[FieldDelta])


def get_db() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def get_anthropic_client() -> Anthropic:
    """Reads ANTHROPIC_API_KEY from the environment (api.db's load_dotenv()
    call, already imported above, has put it there by the time any route
    runs). Overridden in tests with a stub so the vision call never
    actually hits the network or needs a real key."""
    return Anthropic()


@app.get("/patterns/{pattern_id}/versions/{version}/pdf")
def get_pattern_pdf(
    pattern_id: str, version: int, db: sqlite3.Connection = Depends(get_db),
) -> Response:
    row = db.execute(
        "SELECT snapshot_json FROM pattern_versions WHERE pattern_id = ? AND version = ?",
        (pattern_id, version),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="pattern version not found")

    pattern = pattern_from_json(row[0])
    pdf_bytes = render_pattern_pdf(pattern.pieces)
    return Response(content=pdf_bytes, media_type="application/pdf")


@pydantic_dataclass
class ReviseRequest:
    """POST /patterns/{id}/revise's body: the pattern as you want it to
    look after this edit, plus why. `pattern.version`/`pattern.id` are
    ignored in favour of the URL's pattern_id and the next version this
    endpoint computes itself — the caller edits content, not bookkeeping."""
    pattern: Pattern
    reason: str
    fit_notes: str = ""
    source: RevisionSource = RevisionSource.MANUAL


@app.post("/patterns/{pattern_id}/revise")
def revise_pattern(
    pattern_id: str, body: ReviseRequest, db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """PLAN.md Phase 5: diff the submitted pattern against the current
    version, infer what kind of change it is, write a new snapshot and a
    Revision row, bump current_version. Rejects exactly the two cases
    PLAN.md calls out explicitly — an empty diff, and a blank reason —
    because an unexplained or no-op revision is invisible to (or actively
    misleading for) the RAG layer that indexes `reason`/`fit_notes` later.
    """
    row = db.execute("SELECT current_version FROM patterns WHERE id = ?", (pattern_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="pattern not found")
    current_version = row[0]

    snap_row = db.execute(
        "SELECT snapshot_json FROM pattern_versions WHERE pattern_id = ? AND version = ?",
        (pattern_id, current_version),
    ).fetchone()
    if snap_row is None:
        raise HTTPException(
            status_code=500,
            detail=f"{pattern_id}: current_version {current_version} has no stored snapshot",
        )
    old = pattern_from_json(snap_row[0])

    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="reason is required")

    deltas = diff_patterns(old, body.pattern)
    if not deltas:
        raise HTTPException(status_code=400, detail="no changes to revise")

    next_version = current_version + 1
    new_pattern = dataclasses.replace(body.pattern, id=pattern_id, version=next_version)
    change_type = infer_change_type(deltas)
    revision = Revision(
        id=uuid.uuid4().hex,
        pattern_id=pattern_id,
        version=next_version,
        parent_version=current_version,
        change_type=change_type,
        source=body.source,
        deltas=deltas,
        reason=body.reason,
        fit_notes=body.fit_notes,
    )

    db.execute(
        "INSERT INTO pattern_versions (pattern_id, version, snapshot_json) VALUES (?, ?, ?)",
        (pattern_id, next_version, new_pattern.to_json()),
    )
    db.execute("UPDATE patterns SET current_version = ? WHERE id = ?", (next_version, pattern_id))
    db.execute(
        """
        INSERT INTO revisions
            (id, pattern_id, version, parent_version, change_type, source,
             deltas_json, reason, fit_notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision.id, revision.pattern_id, revision.version, revision.parent_version,
            revision.change_type.value, revision.source.value,
            json.dumps(_field_delta_list_adapter.dump_python(deltas, mode="json")),
            revision.reason, revision.fit_notes, revision.created_at,
        ),
    )
    embed_and_store_revision(db, revision)  # same transaction — embedded the moment it exists
    db.commit()

    return _revision_adapter.dump_python(revision, mode="json")


@app.get("/patterns/{pattern_id}/revisions")
def get_revisions(pattern_id: str, db: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    """Timeline, newest first. A pattern that exists but hasn't been
    revised yet (nothing beyond its Phase 4 version 1) correctly returns
    an empty list, not a 404 — that's a real, distinct state from the
    pattern not existing at all, which still 404s below."""
    exists = db.execute("SELECT 1 FROM patterns WHERE id = ?", (pattern_id,)).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="pattern not found")

    rows = db.execute(
        "SELECT id, pattern_id, version, parent_version, change_type, source, "
        "deltas_json, reason, fit_notes, created_at FROM revisions "
        "WHERE pattern_id = ? ORDER BY version DESC",
        (pattern_id,),
    ).fetchall()
    return [_revision_adapter.dump_python(revision_from_row(row), mode="json") for row in rows]


@app.get("/patterns/{pattern_id}/diff")
def diff_pattern_versions(
    pattern_id: str,
    from_version: int = Query(..., alias="from"),
    to_version: int = Query(..., alias="to"),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Aggregate deltas directly between the two actual snapshots, not a
    concatenation of each revision's own stored deltas in between — a
    field nudged twice (15mm -> 20mm -> 25mm across two revisions) should
    show up here as the one change that actually matters end to end
    (15mm -> 25mm), not two redundant entries."""
    from_row = db.execute(
        "SELECT snapshot_json FROM pattern_versions WHERE pattern_id = ? AND version = ?",
        (pattern_id, from_version),
    ).fetchone()
    if from_row is None:
        raise HTTPException(status_code=404, detail=f"{pattern_id}: version {from_version} not found")

    to_row = db.execute(
        "SELECT snapshot_json FROM pattern_versions WHERE pattern_id = ? AND version = ?",
        (pattern_id, to_version),
    ).fetchone()
    if to_row is None:
        raise HTTPException(status_code=404, detail=f"{pattern_id}: version {to_version} not found")

    deltas = diff_patterns(pattern_from_json(from_row[0]), pattern_from_json(to_row[0]))
    return {
        "pattern_id": pattern_id, "from": from_version, "to": to_version,
        "deltas": _field_delta_list_adapter.dump_python(deltas, mode="json"),
    }


@pydantic_dataclass
class AskRequest:
    question: str
    top_k: int = 5
    since: str | None = None  # ISO-8601, inclusive
    until: str | None = None  # ISO-8601, inclusive


@app.post("/patterns/{pattern_id}/ask")
def ask_pattern(
    pattern_id: str, body: AskRequest,
    db: sqlite3.Connection = Depends(get_db), client: Anthropic = Depends(get_anthropic_client),
) -> dict:
    """Phase 6's RAG endpoint: retrieve the pattern's own most relevant
    revisions (local cosine search, no LLM call — see api.rag.embed) and
    have Claude synthesize an answer grounded in their actual deltas.
    Returns the revisions used alongside the answer so it can be checked
    against the real data, not just trusted.

    404s if the pattern doesn't exist at all — a 200 with an explanatory
    "no revision history" answer (see api.rag.answer) is reserved for a
    real pattern that just doesn't have revisions matching the query/date
    range, a meaningfully different state from asking about nothing.
    """
    exists = db.execute("SELECT 1 FROM patterns WHERE id = ?", (pattern_id,)).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="pattern not found")

    return answer_question(
        db, client, pattern_id, body.question,
        top_k=body.top_k, since=body.since, until=body.until,
    )


@app.post("/patterns/{pattern_id}/versions/{version}/mark-sloper")
def mark_sloper(
    pattern_id: str, version: int, db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Mark this exact version as the reference sloper for its own
    garment_type (see schema.py's `reference_slopers` table). A deliberate,
    separate, human-initiated call — PLAN.md Phase 5 is explicit that this
    must never be inferred from revision count or any other heuristic;
    whether a pattern actually fits is a judgment only a muslin can make.

    One sloper per garment_type: marking a new version here replaces
    whichever version held that slot before, it doesn't add another
    candidate. The garment_type itself isn't a separate parameter — it's
    read off the pattern version being marked, since "make this version
    the sloper" and "for which garment type" are the same decision, not
    two independent ones that could disagree.
    """
    row = db.execute(
        "SELECT snapshot_json FROM pattern_versions WHERE pattern_id = ? AND version = ?",
        (pattern_id, version),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="pattern version not found")

    pattern = pattern_from_json(row[0])
    garment_type = pattern.intent.garment_type.value
    marked_at = datetime.now(timezone.utc).isoformat()

    db.execute(
        """
        INSERT INTO reference_slopers (garment_type, pattern_id, version, marked_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(garment_type) DO UPDATE SET
            pattern_id = excluded.pattern_id,
            version = excluded.version,
            marked_at = excluded.marked_at
        """,
        (garment_type, pattern_id, version, marked_at),
    )
    db.commit()

    return {
        "garment_type": garment_type, "pattern_id": pattern_id, "version": version, "marked_at": marked_at,
    }


@app.get("/slopers/{garment_type}")
def get_sloper(garment_type: GarmentType, db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Phase 4's lookup, run before drafting: is there a reference sloper
    for this garment type, and if so, everything `draft_bodice_from_sloper`
    needs to use it (the sloper's own pieces, measurements, and intent —
    not just a pattern_id/version pointer the caller has to go resolve
    itself with a second round trip).

    404, not a null/empty body, when none is marked — PLAN.md's Phase 4
    note says the fallback is "draft from the formulas as today," and a
    plain status check makes that an ordinary try/except at the call site
    instead of a sentinel-value check buried in a 200 response.

    The 500 case (the sloper's pointer outliving the version it points
    at) shouldn't be reachable — nothing in this sketch deletes pattern
    versions — but a clear 500 beats a confusing downstream KeyError the
    day that invariant breaks instead.
    """
    row = db.execute(
        "SELECT pattern_id, version, marked_at FROM reference_slopers WHERE garment_type = ?",
        (garment_type.value,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no reference sloper marked for {garment_type.value}")
    pattern_id, version, marked_at = row

    snap_row = db.execute(
        "SELECT snapshot_json FROM pattern_versions WHERE pattern_id = ? AND version = ?",
        (pattern_id, version),
    ).fetchone()
    if snap_row is None:
        raise HTTPException(
            status_code=500,
            detail=f"reference sloper for {garment_type.value} points at "
                   f"{pattern_id} v{version}, which no longer exists",
        )

    pattern = pattern_from_json(snap_row[0])
    return {
        "garment_type": garment_type.value, "pattern_id": pattern_id, "version": version,
        "marked_at": marked_at, "pattern": pattern.to_dict(),
    }


_DESIGN_INTENT_PROMPT = """\
Look at this croquis sketch of a garment. Respond with ONLY a single JSON \
object, no prose, no markdown code fences, matching exactly this shape:

{
  "garment_type": one of "bodice" | "skirt" | "sleeve" | "trouser" | "dress" | "other",
  "fabric_class": one of "woven" | "stretch_woven" | "knit" (default "woven" if the sketch doesn't make it clear),
  "fit": one of "fitted" | "semi-fitted" | "relaxed" (default "fitted" if unclear),
  "closures": a list of short strings naming visible fastenings, e.g. ["centre_back_zip"] (empty list if none are visible),
  "style_notes": a short free-text description of distinguishing design features you can see \
(necklines, seam lines, sleeve style, collar, etc.)
}

Leave out "ease_bust_mm"/"ease_waist_mm"/"ease_hip_mm" entirely unless the sketch clearly implies \
a looser or closer fit than "fitted" ordinarily means — their defaults are already sensible, \
and a sketch alone is a poor basis for guessing specific ease numbers.
"""

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$")


def _strip_fences(text: str) -> str:
    """Defensive cleanup for a model that wraps its JSON in markdown fences
    despite being told not to (PLAN.md Phase 4 is explicit this will
    happen sometimes) — strips one leading ``` / ```json and one trailing
    ```, leaves anything else untouched."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    return _FENCE_RE.sub("", stripped).strip()


@app.post("/croquis")
async def post_croquis(
    file: UploadFile, client: Anthropic = Depends(get_anthropic_client),
) -> dict[str, Any]:
    """Phase 4: sketch upload -> a stored image + a DRAFT DesignIntent.

    Never creates a pattern and never applies the intent automatically —
    PLAN.md is explicit that vision will misread seam lines and style
    details, and a wrong garment_type would silently send drafting (Phase
    2, or `draft_bodice_from_sloper` once Phase 6's lookup finds a
    sloper) down the wrong path. This is strictly a proposal for a human
    review screen to confirm or correct before anything is drafted.

    On a parse failure (the model didn't return valid JSON matching
    DesignIntent, fences stripped or not), this does NOT retry blindly or
    500 — PLAN.md's instruction is to hand back the raw text for manual
    correction instead, since the image itself was still stored
    successfully; only the proposal needs human help.
    """
    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="empty file")

    CROQUIS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "").suffix or ".png"
    stored_path = CROQUIS_DIR / f"{uuid.uuid4().hex}{suffix}"
    stored_path.write_bytes(image_bytes)
    croquis_ref = str(stored_path.relative_to(CROQUIS_DIR.parent))

    response = client.messages.create(
        model=_VISION_MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": file.content_type or "image/png",
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    },
                },
                {"type": "text", "text": _DESIGN_INTENT_PROMPT},
            ],
        }],
    )
    raw_text = response.content[0].text if response.content else ""
    cleaned = _strip_fences(raw_text)

    try:
        parsed = json.loads(cleaned)
        intent = DesignIntent(**parsed)
    except (json.JSONDecodeError, TypeError, ValidationError) as e:
        return {"croquis_ref": croquis_ref, "intent": None, "parse_error": str(e), "raw_response": raw_text}

    return {
        "croquis_ref": croquis_ref,
        "intent": _design_intent_adapter.dump_python(intent, mode="json"),
        "parse_error": None,
    }
