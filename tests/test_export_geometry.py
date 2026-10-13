"""Tests for api.export.geometry — pure polyline/offset math, checked
against simple shapes and relationships rather than magic numbers."""
import math

from api.drafting.bodice import draft_bodice
from api.export.geometry import (
    bounding_box, mirrored_outline, offset_polyline, piece_outline, point_in_polygon,
    polygon_centroid, signed_area,
)
from api.schema import DesignIntent, FabricClass, GarmentType, Measurements


def _measurements() -> Measurements:
    return Measurements(
        bust=920, waist=740, hip=980, shoulder_width=380,
        back_length=400, bust_height=240, bust_span=180,
    )


def _intent() -> DesignIntent:
    return DesignIntent(GarmentType.BODICE, FabricClass.WOVEN)


# --------------------------------------------------------------------------
# A plain square, independent of any PatternPiece, to pin down offset
# behaviour unambiguously (axis-aligned edges make "outward" obvious).
# --------------------------------------------------------------------------
_SQUARE = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0), (0.0, 0.0)]


def test_square_offset_outward_grows_each_side_by_the_offset_distance():
    offset = offset_polyline(_SQUARE, 10.0)
    xs = sorted({round(p[0], 6) for p in offset})
    ys = sorted({round(p[1], 6) for p in offset})
    assert xs == [-10.0, 110.0]
    assert ys == [-10.0, 110.0]


def test_offset_polygon_area_exceeds_original():
    offset = offset_polyline(_SQUARE, 10.0)
    assert abs(signed_area(offset)) > abs(signed_area(_SQUARE))


def test_zero_offset_is_a_no_op_up_to_floating_point():
    offset = offset_polyline(_SQUARE, 0.0)
    for (ox, oy), (sx, sy) in zip(offset, _SQUARE):
        assert math.isclose(ox, sx, abs_tol=1e-6)
        assert math.isclose(oy, sy, abs_tol=1e-6)


def test_bounding_box_of_square():
    assert bounding_box(_SQUARE) == (0.0, 0.0, 100.0, 100.0)


# --------------------------------------------------------------------------
# Real piece geometry from the Phase 2 drafting output.
# --------------------------------------------------------------------------
def test_piece_outline_is_closed():
    pieces = draft_bodice(_measurements(), _intent())
    for piece in pieces:
        outline = piece_outline(piece)
        assert len(outline) > 3
        assert outline[0] == outline[-1]


def test_piece_outline_visits_every_point_once():
    """Every point actually used as an edge endpoint should appear on the
    outline — if one were missing, the graph walk in piece_outline silently
    dropped it. Points that aren't edge endpoints (dart apexes, and any
    other reference-only landmark a piece may carry, e.g. a bust point used
    only to derive other points' positions) are annotation-only and
    legitimately absent from the outline."""
    pieces = draft_bodice(_measurements(), _intent())
    for piece in pieces:
        outline = {tuple(round(c, 6) for c in p) for p in piece_outline(piece)}
        edge_endpoint_ids = {
            pid for edge in piece.edges for seg in edge.segments for pid in (seg.start, seg.end)
        }
        for point in piece.points:
            if point.id not in edge_endpoint_ids:
                continue  # not part of any edge — annotation-only, not on the outline
            assert (round(point.x, 6), round(point.y, 6)) in outline, (
                f"{piece.id}: {point.id} missing from outline"
            )


def test_cut_line_area_exceeds_stitching_line_area():
    """The seam-allowance offset should expand the piece outward, not
    shrink it — a relationship, not a specific area value."""
    pieces = draft_bodice(_measurements(), _intent())
    for piece in pieces:
        stitching = piece_outline(piece)
        cut = offset_polyline(stitching, piece.seam_allowance_mm)
        assert abs(signed_area(cut)) > abs(signed_area(stitching))


def _segments_cross(p1, p2, p3, p4) -> bool:
    """Proper (transversal) segment crossing, tolerant of near-zero
    orientation values: two genuinely collinear segments (e.g. a dart's
    two straight-line pieces either side of its gap, both built from the
    same underlying shoulder/waist line) produce ccw() values that should
    be exactly 0 but land at ~1e-13 from ordinary floating-point error —
    enough for a strict `> 0` sign comparison to flip unpredictably and
    register a false "crossing" between segments that don't actually
    overlap. Treating anything within `eps` of zero as "on the line"
    (touching, not crossing) avoids that without hiding a real
    intersection, which is orders of magnitude larger at this coordinate
    scale (mm)."""
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])

    def sign(v: float, eps: float = 1e-7) -> int:
        if v > eps:
            return 1
        if v < -eps:
            return -1
        return 0

    d1, d2 = sign(ccw(p3, p4, p1)), sign(ccw(p3, p4, p2))
    d3, d4 = sign(ccw(p1, p2, p3)), sign(ccw(p1, p2, p4))
    if d1 == 0 or d2 == 0 or d3 == 0 or d4 == 0:
        return False
    return (d1 != d2) and (d3 != d4)


def _has_self_intersection(polygon: list[tuple[float, float]]) -> bool:
    pts = polygon[:-1] if polygon and polygon[0] == polygon[-1] else polygon
    n = len(pts)
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if _segments_cross(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]):
                return True
    return False


def _real_measurements() -> Measurements:
    """A real set of body measurements (converted from inches), not a
    guessed 'standard size' — used wherever a test needs geometry close
    to what someone will actually print and cut, since more than one real
    bug here only showed up on real proportions and not on the looser
    generic fixture."""
    return Measurements(
        bust=34 * 25.4, waist=31.5 * 25.4, hip=38 * 25.4, shoulder_width=15 * 25.4,
        back_length=14 * 25.4, bust_height=11 * 25.4, bust_span=7.5 * 25.4,
        neck_circumference=15 * 25.4, shoulder_length=5 * 25.4, back_width=13.0 * 25.4,
        armscye_depth=7.5 * 25.4, nape_to_bust=11 * 25.4,
    )


def test_cut_line_has_no_self_intersections_on_a_tight_concave_curve():
    """A real bug, not a hypothetical: a size-medium-ish set of real body
    measurements produces a front armhole with a concave pull tight
    enough that offsetting it outward by the standard 15mm seam
    allowance, per-vertex, folded the cut line over on itself — a
    self-intersecting polygon. Caught because the resulting PDF wouldn't
    open in one viewer even though it rasterized fine in another; a
    self-intersecting cut line isn't actually cuttable either,
    independent of any particular viewer's tolerance for it."""
    pieces = draft_bodice(_real_measurements(), _intent())
    for piece in pieces:
        stitching = piece_outline(piece)
        cut = offset_polyline(stitching, piece.seam_allowance_mm)
        assert not _has_self_intersection(cut), f"{piece.id}: cut line self-intersects"


# --------------------------------------------------------------------------
# mirrored_outline — the full, cut-on-the-fold shape. A half-piece view
# can look completely correct while hiding a real defect (see
# mirrored_outline's own docstring) — these are the standing checks that
# replace eyeballing a one-off script each time.
# --------------------------------------------------------------------------
def test_mirrored_outline_is_closed_and_bilaterally_symmetric():
    pieces = draft_bodice(_real_measurements(), _intent())
    for piece in pieces:
        full = mirrored_outline(piece)
        assert full[0] == full[-1], f"{piece.id}: mirrored outline isn't closed"

        minx, _, maxx, _ = bounding_box(full)
        assert math.isclose(minx, -maxx, abs_tol=1e-6), f"{piece.id}: mirrored outline isn't symmetric about x=0"


def test_mirrored_outline_has_no_self_intersections():
    """The general case of the fold-line-cusp bug this function was built
    to catch: a visible pinch at the fold line (or anywhere else the two
    mirrored halves meet badly) shows up as a self-intersection once
    actually joined, even though each half's own outline never
    self-intersects on its own."""
    pieces = draft_bodice(_real_measurements(), _intent())
    for piece in pieces:
        full = mirrored_outline(piece)
        assert not _has_self_intersection(full), f"{piece.id}: mirrored (cut-on-fold) outline self-intersects"


# --------------------------------------------------------------------------
# polygon_centroid / point_in_polygon
# --------------------------------------------------------------------------
def test_centroid_of_a_square_is_its_center():
    cx, cy = polygon_centroid(_SQUARE)
    assert math.isclose(cx, 50.0, abs_tol=1e-6)
    assert math.isclose(cy, 50.0, abs_tol=1e-6)


def test_point_in_polygon_classifies_square_correctly():
    assert point_in_polygon((50, 50), _SQUARE) is True
    assert point_in_polygon((150, 50), _SQUARE) is False
    assert point_in_polygon((-10, 50), _SQUARE) is False


def test_bbox_corner_can_be_outside_a_concave_piece_but_centroid_is_inside():
    """The actual bug this pair of functions was added to fix: a bodice
    piece's neckline curve is concave near its own bounding-box corner, so
    (bbox_min_x, bbox_min_y) + a small inset can land outside the polygon
    entirely, even though it looks "inside the box" on paper."""
    piece = draft_bodice(_measurements(), _intent())[0]  # front: has the neckline concavity
    outline = piece_outline(piece)
    sminx, sminy, _, _ = bounding_box(outline)

    bbox_corner_point = (sminx + 6, sminy + 10)
    assert point_in_polygon(bbox_corner_point, outline) is False

    centroid = polygon_centroid(outline)
    assert point_in_polygon(centroid, outline) is True
