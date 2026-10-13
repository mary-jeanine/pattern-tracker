"""Tests for diff_patterns, written before any changes to api/schema.py.

Each test builds the smallest Pattern that exercises one case, rather than
reusing the full bodice fixture everywhere, so a failure points at exactly
one behaviour.
"""
import copy

from api.schema import (
    DesignIntent, Edge, EdgeRole, FabricClass, GarmentType, Measurements,
    Pattern, PatternPiece, Point, Segment, SegmentKind, diff_patterns,
)


def _measurements() -> Measurements:
    return Measurements(
        bust=900, waist=700, hip=950, shoulder_width=380,
        back_length=400, bust_height=240, bust_span=180,
    )


def _intent() -> DesignIntent:
    return DesignIntent(GarmentType.BODICE, FabricClass.WOVEN)


def _pattern(pieces: list[PatternPiece], *, pattern_id: str = "pat_t") -> Pattern:
    return Pattern(
        id=pattern_id, name="Test pattern", version=1,
        measurements=_measurements(), intent=_intent(), pieces=pieces,
    )


# --------------------------------------------------------------------------
# Case 1: reordering a list produces no deltas
# --------------------------------------------------------------------------
def test_reordering_points_produces_no_deltas():
    piece = PatternPiece(
        id="front", name="Front",
        points=[Point("a", 0, 0), Point("b", 10, 0), Point("c", 10, 10)],
    )
    old = _pattern([piece])
    new = copy.deepcopy(old)
    new.pieces[0].points.reverse()

    assert diff_patterns(old, new) == []


# --------------------------------------------------------------------------
# Case 2: renaming a piece produces one add and one remove
# --------------------------------------------------------------------------
def test_renaming_a_piece_produces_one_add_and_one_remove():
    piece = PatternPiece(id="front", name="Front", points=[Point("a", 0, 0)])
    old = _pattern([piece])
    new = copy.deepcopy(old)
    new.pieces[0].id = "front_renamed"

    deltas = diff_patterns(old, new)

    assert len(deltas) == 2
    adds = [d for d in deltas if d.old is None and d.new is not None]
    removes = [d for d in deltas if d.new is None and d.old is not None]
    assert len(adds) == 1
    assert len(removes) == 1
    assert "pieces[front_renamed]" in adds[0].path
    assert "pieces[front]" in removes[0].path


# --------------------------------------------------------------------------
# Case 3: a bezier control point change is detected
# --------------------------------------------------------------------------
def test_bezier_control_point_change_is_detected():
    edge = Edge(
        id="armhole", role=EdgeRole.ARMHOLE,
        segments=[Segment("shoulder", "underarm", SegmentKind.CUBIC,
                           c1=(10, 10), c2=(20, 20))],
    )
    piece = PatternPiece(
        id="front", name="Front",
        points=[Point("shoulder", 0, 0), Point("underarm", 20, 30)],
        edges=[edge],
    )
    old = _pattern([piece])
    new = copy.deepcopy(old)
    new.pieces[0].edges[0].segments[0].c1 = (15, 12)

    deltas = diff_patterns(old, new)

    # Segment now has a stable id (derived from start/end), so this diffs
    # precisely to the one field that changed, same as every other list in
    # the schema — not a whole-segment or whole-list replacement.
    assert len(deltas) == 1
    d = deltas[0]
    assert d.path == "pieces[front].edges[armhole].segments[shoulder->underarm].c1"
    assert d.old == [10, 10]
    assert d.new == [15, 12]


# --------------------------------------------------------------------------
# Case 4: an unchanged pattern diffs to an empty list
# --------------------------------------------------------------------------
def test_unchanged_pattern_diffs_to_empty_list():
    edge = Edge(
        id="armhole", role=EdgeRole.ARMHOLE,
        segments=[Segment("shoulder", "underarm", SegmentKind.CUBIC,
                           c1=(10, 10), c2=(20, 20))],
    )
    piece = PatternPiece(
        id="front", name="Front",
        points=[Point("shoulder", 0, 0), Point("underarm", 20, 30)],
        edges=[edge],
    )
    old = _pattern([piece])
    new = copy.deepcopy(old)

    assert diff_patterns(old, new) == []
