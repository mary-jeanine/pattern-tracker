"""Smoke tests for api.export.pdf. These check structure (valid PDF bytes,
page count, a calibration square sized exactly 50mm) — NOT physical print
accuracy, which per CLAUDE.md only a printed page and a ruler can confirm.
"""
import math

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm

from api.drafting.bodice import draft_bodice
from api.export.pdf import (
    _CALIBRATION_SQUARE_MM, _DEFAULT_MARGIN_MM,
    _canvas_bbox, _layout_pieces, _seam_coordinate, _tile_starts,
    render_pattern_pdf,
)
from api.schema import DesignIntent, FabricClass, GarmentType, Measurements


def _measurements() -> Measurements:
    return Measurements(
        bust=920, waist=740, hip=980, shoulder_width=380,
        back_length=400, bust_height=240, bust_span=180,
    )


def _intent() -> DesignIntent:
    return DesignIntent(GarmentType.BODICE, FabricClass.WOVEN)


def test_output_is_valid_pdf_bytes():
    pieces = draft_bodice(_measurements(), _intent())
    pdf_bytes = render_pattern_pdf(pieces)
    assert pdf_bytes.startswith(b"%PDF-")
    assert pdf_bytes.rstrip().endswith(b"%%EOF")


def test_unknown_page_size_rejected():
    pieces = draft_bodice(_measurements(), _intent())
    try:
        render_pattern_pdf(pieces, page_size="tabloid")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_page_count_matches_tiling_math_plus_overview_page():
    pieces = draft_bodice(_measurements(), _intent())
    pdf_bytes = render_pattern_pdf(pieces, page_size="letter")

    # Independently compute expected page count from the same geometry,
    # rather than just asserting "at least one page" — this would catch an
    # off-by-one in render_pattern_pdf's own tiling loop. +1 for the
    # assembly overview page (always page 1) +1 for the cut-on-fold preview
    # page (page 2, whenever any piece is cut_mirrored — see
    # render_pattern_pdf and CLAUDE.md's fold-line verification note).
    page_w_mm, page_h_mm = letter[0] / mm, letter[1] / mm
    usable_w = page_w_mm - 2 * _DEFAULT_MARGIN_MM
    usable_h = page_h_mm - 2 * _DEFAULT_MARGIN_MM
    step_w = usable_w  # zero overlap — see render_pattern_pdf
    step_h = usable_h
    minx, miny, maxx, maxy = _canvas_bbox(_layout_pieces(pieces))
    expected_tiles = (
        len(_tile_starts(maxx - minx, usable_w, step_w))
        * len(_tile_starts(maxy - miny, usable_h, step_h))
    )
    expected_extra_pages = 1 + (1 if any(p.cut_mirrored for p in pieces) else 0)

    # Lightweight page-count check without a PDF-parsing dependency: each
    # page object is tagged /Type /Page (not /Pages, the container) once.
    page_count = (
        pdf_bytes.count(b"/Type /Page\n")
        + pdf_bytes.count(b"/Type /Page/")
        + pdf_bytes.count(b"/Type /Page ")
    )
    assert page_count == expected_tiles + expected_extra_pages


def test_overview_page_is_first_and_all_three_calibration_squares_appear_once():
    pieces = draft_bodice(_measurements(), _intent())
    pdf_bytes = render_pattern_pdf(pieces, page_size="letter")

    assert pdf_bytes.count(b"50mm square") == 1
    assert pdf_bytes.count(b"1 inch square") == 1
    assert pdf_bytes.count(b"1cm square") == 1
    assert pdf_bytes.count(b"Assembly overview") == 1
    assert pdf_bytes.index(b"Assembly overview") < pdf_bytes.index(b"50mm square")


def test_tile_pages_carry_a_code_and_trim_lines_reference_their_neighbour():
    pieces = draft_bodice(_measurements(), _intent())
    pdf_bytes = render_pattern_pdf(pieces, page_size="letter")

    # Every tile page prints its own lettered/numbered code (A1, A2, ...)
    assert b"A1" in pdf_bytes
    # The first tile has no neighbour yet, so no "tapes to" caption at all
    # is a legitimate single-tile outcome; only assert the mechanism exists
    # when there's more than one tile to reference.
    if pdf_bytes.count(b"/Type /Page") > 2:
        assert b"trim" in pdf_bytes and b"tapes to" in pdf_bytes


def test_piece_label_includes_seam_allowance_statement():
    pieces = draft_bodice(_measurements(), _intent())
    pdf_bytes = render_pattern_pdf(pieces, page_size="letter")
    assert b"Seam allowance" in pdf_bytes
    assert b"included in cut line" in pdf_bytes


def test_label_position_is_actually_inside_the_polygon_not_just_its_bbox():
    """Regression test for two bugs in sequence: the label was first placed
    relative to the piece's raw local origin (outside the piece entirely),
    then "fixed" to a bounding-box corner — which is still outside the
    polygon for a concave shape like a neckline curve. point_in_polygon is
    the real check; a bbox-range check would have passed both bugs."""
    from api.export.geometry import point_in_polygon
    from api.export.pdf import _layout_pieces

    pieces = draft_bodice(_measurements(), _intent())
    for pl in _layout_pieces(pieces):
        assert point_in_polygon(pl.label_anchor, pl.stitching), (
            f"{pl.piece.id}: label anchor {pl.label_anchor} is outside the stitching outline"
        )


def test_a4_and_letter_both_render():
    pieces = draft_bodice(_measurements(), _intent())
    for size in ("letter", "a4"):
        pdf_bytes = render_pattern_pdf(pieces, page_size=size)
        assert pdf_bytes.startswith(b"%PDF-")


def test_tile_starts_single_tile_when_content_fits():
    assert _tile_starts(total_mm=100, usable_mm=200, step_mm=180) == [0.0]


def test_tile_starts_multiple_tiles_cover_the_full_span():
    starts = _tile_starts(total_mm=500, usable_mm=200, step_mm=180)
    assert starts[0] == 0.0
    assert starts[-1] + 200 >= 500
    # consecutive tiles overlap by exactly usable - step
    for a, b in zip(starts, starts[1:]):
        assert b - a == 180


def test_calibration_square_is_exactly_50mm():
    assert _CALIBRATION_SQUARE_MM == 50.0


def test_seam_coordinate_is_the_same_formula_on_every_edge():
    """No more special-casing for shared vs. outer edges — the trim line is
    simply this page's own window boundary, uniformly, everywhere. This is
    exactly the property that was broken before: outer edges used a
    ~0mm cosmetic inset while shared edges used overlap/2, producing two
    different-looking margins on the same page. (The physical "margin_mm
    from the page edge" comes from to_pt()'s own offset, applied the same
    way regardless of which edge this is — see _seam_coordinate's
    docstring for why adding margin_mm again here would double it.)"""
    usable_mm = 190.0
    window_start = 37.0
    assert _seam_coordinate(window_start, usable_mm, far_side=False) == window_start
    assert _seam_coordinate(window_start, usable_mm, far_side=True) == window_start + usable_mm


def test_seam_coordinate_matches_between_adjacent_tiles_with_zero_overlap():
    """The core correctness property behind the half-diamond match marks:
    two pages that share an edge must compute the exact same canvas
    coordinate for the seam, independently, each from its own window —
    otherwise a mark cut in half by the real paper cut wouldn't reconstruct
    into one diamond when the two sheets are taped together. This is only
    exact (not merely close) because render_pattern_pdf tiles with ZERO
    overlap — page B's window starts exactly where page A's ends."""
    usable_mm = 190.0
    step_mm = usable_mm  # zero overlap
    window_a = 0.0
    window_b = window_a + step_mm  # the tile immediately to the right of A

    a_far_edge = _seam_coordinate(window_a, usable_mm, far_side=True)
    b_near_edge = _seam_coordinate(window_b, usable_mm, far_side=False)
    assert a_far_edge == b_near_edge  # exact, not approximate — no float error to tolerate


def test_piece_seam_crossings_one_per_piece_that_actually_spans_the_seam():
    from api.export.geometry import bounding_box
    from api.export.pdf import _layout_pieces, _piece_seam_crossings

    pieces = draft_bodice(_measurements(), _intent())
    placed = _layout_pieces(pieces)

    # A vertical seam through the middle of the first piece's cut outline:
    # exactly one piece spans it (the pieces are laid out side by side with
    # a gap, so a seam inside piece 0's span can't also be inside piece 1's).
    minx, _, maxx, _ = bounding_box(placed[0].cut)
    seam_x = placed[0].offset_x + (minx + maxx) / 2
    crossings = _piece_seam_crossings(placed, seam_x, window_lo=-10_000, window_hi=10_000, vertical=True)
    assert len(crossings) == 1

    # A seam far to the right of every piece: nothing spans it, so no marks.
    far_right_x = max(pl.offset_x + bounding_box(pl.cut)[2] for pl in placed) + 1000
    assert _piece_seam_crossings(placed, far_right_x, -10_000, 10_000, vertical=True) == []


def test_clamp_label_x_leaves_text_that_already_fits_alone():
    from api.export.pdf import _clamp_label_x

    assert _clamp_label_x(label_x=10.0, text_width=50.0, right_limit=200.0) == 10.0


def test_clamp_label_x_slides_overflowing_text_left_to_fit():
    from api.export.pdf import _clamp_label_x

    # text starting at 180 with width 50 would end at 230, past right_limit=200
    result = _clamp_label_x(label_x=180.0, text_width=50.0, right_limit=200.0, margin=2.0)
    assert result + 50.0 == 200.0 - 2.0  # ends exactly at the margin, never past it


def test_clamp_label_y_offset_grants_full_offset_when_room_allows():
    from api.export.pdf import _clamp_label_y_offset

    # anchor at y=50, bottom seam at y=200: plenty of room for a 35mm offset
    assert _clamp_label_y_offset(anchor_y=50.0, desired_offset=35.0, bottom_limit=200.0) == 35.0


def test_clamp_label_y_offset_shrinks_when_seam_is_close():
    from api.export.pdf import _clamp_label_y_offset

    # anchor at y=190, bottom seam at y=200, margin=5: only 5mm of room left,
    # nowhere near the desired 35mm — this is the bug that dropped the label
    # off the page entirely before this clamp existed.
    result = _clamp_label_y_offset(anchor_y=190.0, desired_offset=35.0, bottom_limit=200.0, margin=5.0)
    assert result == 5.0


def test_clamp_label_y_offset_never_goes_negative():
    from api.export.pdf import _clamp_label_y_offset

    # anchor already past the bottom seam (shouldn't happen given the
    # anchor-on-this-page guard, but the clamp itself must still be safe)
    result = _clamp_label_y_offset(anchor_y=210.0, desired_offset=35.0, bottom_limit=200.0, margin=5.0)
    assert result == 0.0


def test_complementary_half_diamonds_share_their_flat_edge_and_form_a_full_diamond():
    """The actual correctness property behind 'triangles that become a
    diamond when taped': two pages drawing opposite halves of the same
    seam point must produce triangles that share both flat-edge vertices
    exactly, with apexes on opposite sides — so the four distinct points
    together are exactly a diamond's four vertices, not some other shape."""
    from api.export.pdf import _half_diamond_vertices

    seam, along, size = 100.0, 50.0, 6.0

    for vertical in (True, False):
        left_verts = _half_diamond_vertices(seam, along, size, vertical, apex_sign=1)
        right_verts = _half_diamond_vertices(seam, along, size, vertical, apex_sign=-1)

        flat_a_l, apex_l, flat_b_l = left_verts
        flat_a_r, apex_r, flat_b_r = right_verts

        # the flat edge (the seam itself) is identical on both halves
        assert flat_a_l == flat_a_r
        assert flat_b_l == flat_b_r
        # the two apexes are reflections of each other across the seam
        assert apex_l != apex_r

        all_points = {flat_a_l, flat_b_l, apex_l, apex_r}
        assert len(all_points) == 4  # four distinct vertices, not an overlap

        r = size / 2
        if vertical:
            assert apex_l == (seam + r, along)
            assert apex_r == (seam - r, along)
        else:
            assert apex_l == (along, seam + r)
            assert apex_r == (along, seam - r)


def test_half_diamonds_on_adjacent_tiles_actually_reconstruct_a_diamond():
    """End-to-end version of the property above, using the real seam
    coordinates two adjacent tiles would compute (zero overlap — see
    render_pattern_pdf) rather than an arbitrary shared seam value."""
    from api.export.pdf import _half_diamond_vertices

    usable_mm, size = 190.0, 6.0
    window_a, window_b = 0.0, usable_mm  # B starts exactly where A ends

    seam_a = _seam_coordinate(window_a, usable_mm, far_side=True)
    seam_b = _seam_coordinate(window_b, usable_mm, far_side=False)
    assert seam_a == seam_b  # the shared cut line both pages agree on

    along = 75.0
    a_verts = _half_diamond_vertices(seam_a, along, size, True, apex_sign=1)
    b_verts = _half_diamond_vertices(seam_b, along, size, True, apex_sign=-1)
    flat_a_l, apex_a, flat_b_l = a_verts
    flat_a_r, apex_b, flat_b_r = b_verts
    assert flat_a_l == flat_a_r
    assert flat_b_l == flat_b_r
    assert len({flat_a_l, flat_b_l, apex_a, apex_b}) == 4
