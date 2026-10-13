"""
Pattern schema: the single data model shared by
  1. croquis + measurements -> initial pattern (version 1)
  2. PDF export (geometry -> drawn pieces)
  3. revision logging + RAG (structured deltas + human reasoning)

Conventions
- All lengths are millimetres. Convert only at the UI / PDF edge.
- Coordinates: origin at the piece's top-left of its bounding box, x right, y down.
- Every addressable thing has a stable string id. Revisions point at ids, never
  at list positions, so "bust dart" stays "bust dart" when other things move.
- Geometry stores SEAM LINES (stitching line). Seam allowance is a number on the
  piece and is offset at export time, so changing it is a one-field revision.
"""
from __future__ import annotations

import json
import re
from dataclasses import field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------
class GarmentType(str, Enum):
    BODICE = "bodice"
    SKIRT = "skirt"
    SLEEVE = "sleeve"
    TROUSER = "trouser"
    DRESS = "dress"
    OTHER = "other"


class FabricClass(str, Enum):
    WOVEN = "woven"
    STRETCH_WOVEN = "stretch_woven"
    KNIT = "knit"


class EdgeRole(str, Enum):
    SEAM = "seam"            # sewn to another piece
    FOLD = "fold"            # place on fold, no seam allowance
    HEM = "hem"
    NECKLINE = "neckline"
    ARMHOLE = "armhole"
    CLOSURE = "closure"      # centre back / front opening


class SegmentKind(str, Enum):
    LINE = "line"
    CUBIC = "cubic"          # cubic bezier, uses c1 and c2


class ChangeType(str, Enum):
    DART = "dart"
    EASE = "ease"
    LENGTH = "length"
    WIDTH = "width"
    CURVE = "curve"
    SEAM_ALLOWANCE = "seam_allowance"
    NOTCH = "notch"
    GRAINLINE = "grainline"
    MEASUREMENT = "measurement"
    PIECE_ADDED = "piece_added"
    PIECE_REMOVED = "piece_removed"
    OTHER = "other"


class RevisionSource(str, Enum):
    CROQUIS_DRAFT = "croquis_draft"   # the initial AI-generated version
    MANUAL = "manual"                 # you edited it
    AI_ADJUST = "ai_adjust"           # you asked the assistant to change it
    MUSLIN_FITTING = "muslin_fitting"  # edit driven by a physical fit test


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------
@dataclass
class Point:
    id: str                  # e.g. "bust_point", "shoulder_tip"
    x: float
    y: float


@dataclass
class Segment:
    """One piece of an edge, from `start` point id to `end` point id."""
    start: str
    end: str
    kind: SegmentKind = SegmentKind.LINE
    c1: Optional[tuple[float, float]] = None   # bezier control points
    c2: Optional[tuple[float, float]] = None
    # Stable id for diffing, keyed like every other list in this schema.
    # Derived from start/end rather than author-assigned, so existing call
    # sites don't need to change — and it inherits the same stability
    # guarantee CLAUDE.md gives point ids (a renamed point needs a
    # migration anyway, and that migration already has to touch segment
    # endpoints, so a derived segment id doesn't add a new failure mode).
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"{self.start}->{self.end}"


@dataclass
class Edge:
    id: str                  # e.g. "armhole", "side_seam"
    role: EdgeRole
    segments: list[Segment]
    joins_to: Optional[str] = None   # "piece_id.edge_id" for seam matching


@dataclass
class Dart:
    id: str                  # e.g. "bust_dart"
    apex: str                # point id
    leg_a: str               # point id
    leg_b: str               # point id

    def intake_mm(self, points: dict[str, Point]) -> float:
        a, b = points[self.leg_a], points[self.leg_b]
        return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


@dataclass
class Notch:
    id: str
    point: str               # point id on an edge
    count: int = 1           # single / double / triple notch


@dataclass
class Grainline:
    start: str               # point ids
    end: str
    bias: bool = False


@dataclass
class PatternPiece:
    id: str                  # e.g. "bodice_front"
    name: str
    cut_count: int = 1       # how many to cut
    cut_mirrored: bool = True
    seam_allowance_mm: float = 15.0
    hem_allowance_mm: float = 30.0
    points: list[Point] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    darts: list[Dart] = field(default_factory=list)
    notches: list[Notch] = field(default_factory=list)
    grainline: Optional[Grainline] = None

    def point_map(self) -> dict[str, Point]:
        return {p.id: p for p in self.points}


# --------------------------------------------------------------------------
# Body + pattern
# --------------------------------------------------------------------------
@dataclass
class Measurements:
    """Body measurements in mm. Extend as garment types need more."""
    bust: float
    waist: float
    hip: float
    shoulder_width: float
    back_length: float            # nape to waist
    bust_height: float            # shoulder to bust point
    bust_span: float              # apex to apex
    arm_length: Optional[float] = None
    upper_arm: Optional[float] = None
    garment_length: Optional[float] = None
    back_width: Optional[float] = None        # FULL across-back, shoulder-blade level (halved in the draft)
    neck_circumference: Optional[float] = None
    waist_to_hip: Optional[float] = None
    crotch_depth: Optional[float] = None      # waist to crotch
    thigh_circumference: Optional[float] = None
    wrist_circumference: Optional[float] = None
    armscye_depth: Optional[float] = None     # nape, straight down to the underarm level
    shoulder_length: Optional[float] = None   # one shoulder seam, neck point to shoulder tip
    nape_to_bust: Optional[float] = None      # nape, straight down to the bust line (not shoulder-to-bust)


@dataclass
class DesignIntent:
    """What the croquis implies. Filled by the vision step, editable by you."""
    garment_type: GarmentType
    fabric_class: FabricClass = FabricClass.WOVEN
    fit: str = "fitted"            # fitted / semi-fitted / relaxed
    closures: list[str] = field(default_factory=list)   # "centre_back_zip", ...
    style_notes: str = ""          # free text from the croquis, e.g. "princess seams"
    ease_bust_mm: float = 50.0
    ease_waist_mm: float = 20.0   # matches the bodice tutorial's own waist-dart-calc ease (2cm)
    ease_hip_mm: float = 40.0


@dataclass
class Pattern:
    id: str
    name: str
    version: int
    measurements: Measurements
    intent: DesignIntent
    pieces: list[PatternPiece] = field(default_factory=list)
    croquis_ref: Optional[str] = None      # path / blob id of the uploaded sketch

    # ---- serialisation ----
    def to_dict(self) -> dict[str, Any]:
        return _pattern_adapter.dump_python(self, mode="json")

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), **kw)

    def piece(self, piece_id: str) -> PatternPiece:
        for p in self.pieces:
            if p.id == piece_id:
                return p
        raise KeyError(piece_id)


# Built once, used by Pattern.to_dict() for enum/tuple-safe JSON-mode dumping
# (pydantic dataclasses don't get BaseModel's .model_dump(), so this is the
# supported way to get the same JSON-safe dict diff_patterns walks).
_pattern_adapter = TypeAdapter(Pattern)


def pattern_from_json(snapshot_json: str) -> Pattern:
    """Inverse of Pattern.to_json() — reconstructs a Pattern from a
    pattern_versions.snapshot_json row, validating it against the schema."""
    return _pattern_adapter.validate_json(snapshot_json)


# --------------------------------------------------------------------------
# Revisions (this is what the RAG layer indexes)
# --------------------------------------------------------------------------
@dataclass
class FieldDelta:
    """One concrete change, addressed by a stable path into the pattern dict."""
    path: str                # "pieces[bodice_front].seam_allowance_mm"
    old: Any
    new: Any


@dataclass
class Revision:
    id: str
    pattern_id: str
    version: int             # the version this revision PRODUCED
    parent_version: Optional[int]
    change_type: ChangeType
    source: RevisionSource
    deltas: list[FieldDelta]
    reason: str = ""         # why you made it  -> embedded
    fit_notes: str = ""      # what you observed -> embedded
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def embedding_text(self) -> str:
        """The natural-language blob the RAG layer embeds. Numbers are included
        as words so retrieval can match on them, but the authoritative values
        always come from `deltas`, not from the embedding."""
        parts = [
            f"{self.change_type.value} change via {self.source.value}",
            f"on pattern {self.pattern_id} v{self.parent_version}->v{self.version}",
        ]
        for d in self.deltas:
            parts.append(f"{d.path}: {d.old} -> {d.new}")
        if self.reason:
            parts.append(f"Reason: {self.reason.rstrip('. ')}")
        if self.fit_notes:
            parts.append(f"Fit notes: {self.fit_notes.rstrip('. ')}")
        return ". ".join(parts)


_field_delta_list_adapter = TypeAdapter(list[FieldDelta])


def revision_from_row(row: tuple) -> Revision:
    """Inverse of how main.py/rag write a `revisions` row — reconstructs a
    Revision from the 10 columns in schema order (id, pattern_id, version,
    parent_version, change_type, source, deltas_json, reason, fit_notes,
    created_at), parsing deltas_json back into typed FieldDelta objects.
    Shared by the revise/revisions endpoints and the RAG retrieval layer so
    there's exactly one place that knows the row shape."""
    (id_, pattern_id, version, parent_version, change_type, source,
     deltas_json, reason, fit_notes, created_at) = row
    return Revision(
        id=id_, pattern_id=pattern_id, version=version, parent_version=parent_version,
        change_type=ChangeType(change_type), source=RevisionSource(source),
        deltas=_field_delta_list_adapter.validate_python(json.loads(deltas_json)),
        reason=reason, fit_notes=fit_notes, created_at=created_at,
    )


@dataclass
class ReferenceSloper:
    """One row of the `reference_slopers` table (Phase 6): which specific,
    already-fitted pattern version counts as the trusted starting point
    for a garment type. Written only by Phase 5's `mark-sloper` endpoint
    — a human judgment call, never inferred from revision history."""
    garment_type: GarmentType
    pattern_id: str
    version: int
    marked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# --------------------------------------------------------------------------
# Structural diff: pattern vN vs pattern vN+1 -> list[FieldDelta]
# --------------------------------------------------------------------------
# Lists of objects with an "id" are keyed by id so a reorder is not a change
# and a moved bust dart is reported as exactly that.
def diff_patterns(old: Pattern, new: Pattern) -> list[FieldDelta]:
    out: list[FieldDelta] = []
    _walk(old.to_dict(), new.to_dict(), "", out)
    return out


def _walk(a: Any, b: Any, path: str, out: list[FieldDelta]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k == "version":          # bookkeeping, not a design change
                continue
            sub = f"{path}.{k}" if path else k
            if k not in a:
                out.append(FieldDelta(sub, None, b[k]))
            elif k not in b:
                out.append(FieldDelta(sub, a[k], None))
            else:
                _walk(a[k], b[k], sub, out)
    elif isinstance(a, list) and isinstance(b, list):
        if _all_have_id(a) and _all_have_id(b):
            ia, ib = {x["id"]: x for x in a}, {x["id"]: x for x in b}
            for i in sorted(set(ia) | set(ib)):
                sub = f"{path}[{i}]"
                if i not in ia:
                    out.append(FieldDelta(sub, None, ib[i]))
                elif i not in ib:
                    out.append(FieldDelta(sub, ia[i], None))
                else:
                    _walk(ia[i], ib[i], sub, out)
        elif a != b:
            out.append(FieldDelta(path, a, b))
    elif a != b:
        out.append(FieldDelta(path, a, b))


def _all_have_id(xs: list) -> bool:
    return bool(xs) and all(isinstance(x, dict) and "id" in x for x in xs)


# --------------------------------------------------------------------------
# Change-type inference: list[FieldDelta] -> ChangeType
# --------------------------------------------------------------------------
_PIECE_WHOLE_RE = re.compile(r"^pieces\[[^\]]+\]$")
_POINT_ID_RE = re.compile(r"\.points\[([^\]]+)\]")
_POINT_COORD_RE = re.compile(r"\.points\[[^\]]+\]\.(x|y)$")


def infer_change_type(deltas: list[FieldDelta]) -> ChangeType:
    """Classify a revision's deltas into one ChangeType.

    Categories are checked in priority order — the first one with a
    matching delta wins. This matters when a revision mixes categories: in
    tests/example_bodice.py, widening a dart also bumps seam_allowance_mm
    in the same edit, and a sewist would call that whole thing a DART
    change, not a seam-allowance change. DART is checked before
    SEAM_ALLOWANCE/EASE/MEASUREMENT so the dominant, structural change
    wins over incidental ones.

    WIDTH/LENGTH is the lowest-confidence category — a bare point x/y
    delta doesn't really say "the garment got wider," only "this
    coordinate moved" — so it only fires when every point-coordinate delta
    present agrees on one axis, and falls through to OTHER otherwise
    rather than guess (see CLAUDE.md: don't report confident nonsense).
    """
    if any(_is_piece_added(d) for d in deltas):
        return ChangeType.PIECE_ADDED
    if any(_is_piece_removed(d) for d in deltas):
        return ChangeType.PIECE_REMOVED
    if any(_is_dart(d) for d in deltas):
        return ChangeType.DART
    if any(".grainline" in d.path for d in deltas):
        return ChangeType.GRAINLINE
    if any(".notches[" in d.path for d in deltas):
        return ChangeType.NOTCH
    if any("seam_allowance_mm" in d.path for d in deltas):
        return ChangeType.SEAM_ALLOWANCE
    if any("ease_" in d.path for d in deltas):
        return ChangeType.EASE
    if any(d.path.startswith("measurements.") for d in deltas):
        return ChangeType.MEASUREMENT
    if any(".segments[" in d.path for d in deltas):
        return ChangeType.CURVE
    width_or_length = _infer_width_or_length(deltas)
    if width_or_length is not None:
        return width_or_length
    return ChangeType.OTHER


def _is_piece_added(d: FieldDelta) -> bool:
    return d.old is None and bool(_PIECE_WHOLE_RE.match(d.path))


def _is_piece_removed(d: FieldDelta) -> bool:
    return d.new is None and bool(_PIECE_WHOLE_RE.match(d.path))


def _is_dart(d: FieldDelta) -> bool:
    if ".darts[" in d.path:
        return True
    # a point id that reads as a dart leg/apex, e.g. points[dart_leg_a]
    m = _POINT_ID_RE.search(d.path)
    return bool(m) and "dart" in m.group(1).lower()


def _infer_width_or_length(deltas: list[FieldDelta]) -> Optional[ChangeType]:
    axes = {m.group(1) for d in deltas if (m := _POINT_COORD_RE.search(d.path))}
    if len(axes) != 1:
        return None
    return ChangeType.WIDTH if axes == {"x"} else ChangeType.LENGTH


# --------------------------------------------------------------------------
# SQLite DDL (personal-scale storage)
# --------------------------------------------------------------------------
DDL = """
CREATE TABLE IF NOT EXISTS patterns (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

-- Full geometry snapshot per version. Cheap at this scale, and means any
-- version can be re-exported to PDF without replaying revisions.
CREATE TABLE IF NOT EXISTS pattern_versions (
    pattern_id    TEXT NOT NULL REFERENCES patterns(id),
    version       INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY (pattern_id, version)
);

CREATE TABLE IF NOT EXISTS revisions (
    id             TEXT PRIMARY KEY,
    pattern_id     TEXT NOT NULL REFERENCES patterns(id),
    version        INTEGER NOT NULL,
    parent_version INTEGER,
    change_type    TEXT NOT NULL,
    source         TEXT NOT NULL,
    deltas_json    TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    fit_notes      TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);

-- Only the text side is embedded. Vector stored as a BLOB (float32 array).
CREATE TABLE IF NOT EXISTS revision_embeddings (
    revision_id TEXT PRIMARY KEY REFERENCES revisions(id),
    model       TEXT NOT NULL,
    vector      BLOB NOT NULL
);

-- One reference sloper per garment type (Phase 6) -- PRIMARY KEY on
-- garment_type, not an autoincrement id, so marking a new sloper for a
-- type is a plain upsert that replaces the old one rather than needing a
-- separate "is this the current one" flag to maintain. Points at a
-- specific version, not just a pattern, since the sloper is "this
-- fitted snapshot," and later revisions to the same pattern keep
-- drafting from the version actually marked, not whatever it's since
-- become.
CREATE TABLE IF NOT EXISTS reference_slopers (
    garment_type TEXT PRIMARY KEY,
    pattern_id   TEXT NOT NULL REFERENCES patterns(id),
    version      INTEGER NOT NULL,
    marked_at    TEXT NOT NULL,
    FOREIGN KEY (pattern_id, version) REFERENCES pattern_versions(pattern_id, version)
);
"""
