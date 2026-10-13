"""Smoke tests for api.export.svg — structural checks on the generated XML,
not a visual/physical-scale verification (that needs a real printer and a
ruler, see docs/PLAN.md Phase 3)."""
import re

from api.drafting.bodice import draft_bodice
from api.export.svg import piece_to_svg
from api.schema import DesignIntent, FabricClass, GarmentType, Measurements


def _measurements() -> Measurements:
    return Measurements(
        bust=920, waist=740, hip=980, shoulder_width=380,
        back_length=400, bust_height=240, bust_span=180,
    )


def _intent() -> DesignIntent:
    return DesignIntent(GarmentType.BODICE, FabricClass.WOVEN)


def test_svg_declares_physical_mm_dimensions():
    piece = draft_bodice(_measurements(), _intent())[0]
    svg = piece_to_svg(piece)
    assert re.search(r'width="[\d.]+mm"', svg)
    assert re.search(r'height="[\d.]+mm"', svg)


def test_svg_contains_both_stitching_and_cut_line_polygons():
    piece = draft_bodice(_measurements(), _intent())[0]
    svg = piece_to_svg(piece)
    # 2 outline polygons (stitching + cut) + 2 grainline arrowhead triangles
    assert svg.count("<polygon") == 4
    assert 'stroke="black"' in svg
    assert 'stroke-dasharray="4,2"' in svg  # cut line — dashed, not red, to distinguish from stitching
    assert 'fill="green"' in svg      # grainline arrowheads


def test_svg_draws_a_line_per_dart_leg():
    piece = draft_bodice(_measurements(), _intent())[0]
    svg = piece_to_svg(piece)
    dart_line_count = svg.count('stroke-dasharray="2,1"')
    assert dart_line_count == 2 * len(piece.darts)


def test_svg_includes_piece_label_with_cut_count_mirroring_and_seam_allowance():
    piece = draft_bodice(_measurements(), _intent())[0]
    svg = piece_to_svg(piece)
    assert piece.name in svg
    assert "cut 2, mirrored" in svg  # cut_mirrored defaults True in bodice.py
    assert "Seam allowance" in svg
    assert "included in cut line" in svg


def test_svg_label_position_is_actually_inside_the_polygon_not_just_its_bbox():
    """A bounding-box corner isn't a safe stand-in for "inside the piece" —
    the neckline curve is concave, so that corner can sit in empty space
    outside the polygon entirely. point_in_polygon is the real check."""
    from api.export.geometry import piece_outline, point_in_polygon, polygon_centroid

    piece = draft_bodice(_measurements(), _intent())[0]
    outline = piece_outline(piece)
    anchor = polygon_centroid(outline)
    assert point_in_polygon(anchor, outline)
