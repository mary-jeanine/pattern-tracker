"""PatternPiece geometry -> a tiled, physically-scaled PDF (Letter or A4).

Draws directly via reportlab (not through svg.py's SVG output) — reportlab
is already a Phase 0 dependency and draws mm-exact vector graphics on its
own; converting rendered SVG to PDF would need an extra library Phase 0
never declared.

CLAUDE.md / docs/PLAN.md are explicit that a 50mm calibration square on
page 1 is mandatory, and that it's the only real verification: print at
100% / actual size and measure it with a ruler before trusting anything
else on the page. This module cannot verify that itself — it can only draw
the squares at the dimensions they claim. A 1 inch and a 1cm square are
drawn alongside the mandatory 50mm one so a unit-conversion scaling error
(as opposed to a uniform mis-scale) is also catchable at a glance.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from api.export.geometry import (
    bounding_box, dart_lines, mirrored_outline, offset_polyline, piece_outline, polygon_centroid,
)
from api.schema import PatternPiece

_PAGE_SIZES = {"letter": letter, "a4": A4}
_DEFAULT_MARGIN_MM = 12.7  # 0.5 inch, exactly (1in = 25.4mm) — the trim
                           # line sits here, uniformly, on every edge of
                           # every page (see _seam_coordinate). Tiles are
                           # laid out with ZERO overlap (see
                           # render_pattern_pdf) so that two adjacent
                           # pages' trim lines are the exact same canvas
                           # coordinate, not merely close — required for
                           # the half-diamond match marks to actually
                           # reconstruct a whole diamond when the pages are
                           # cut along that line and taped together.
_CALIBRATION_SQUARE_MM = 50.0   # mandatory per CLAUDE.md
_CALIBRATION_INCH_MM = 25.4
_CALIBRATION_CM_MM = 10.0
_PIECE_GAP_MM = 20.0
_REGISTRATION_SPACING_MM = 50.0
_REGISTRATION_MARK_MM = 4.0
_MAX_GRID_ITERATIONS = 1000
_OVERVIEW_HEADING_MM = 12.0
_OVERVIEW_FOOTER_MM = 75.0  # reserved for the three true-scale calibration squares + labels
_SEAM_DASH = [4, 2]         # black — cut line (seam allowance), dashed to distinguish from stitching
_DART_DASH = [2, 1]
_TRIM_DASH = [6, 3]         # light grey — paper trim line, deliberately distinct from both
_GRAINLINE_ARROW_MM = 4.0
_GRAINLINE_LENGTH_MM = 60.0
_LABEL_OFFSET_FROM_GRAINLINE_MM = 35.0
_DIAMOND_SIZE_MM = 6.0


@dataclass
class _Placed:
    piece: PatternPiece
    offset_x: float
    offset_y: float
    stitching: list[tuple[float, float]]
    cut: list[tuple[float, float]]
    label_anchor: tuple[float, float]  # piece-local point, guaranteed inside the stitching outline


def _layout_pieces(pieces: list[PatternPiece]) -> list[_Placed]:
    """Place pieces left to right on one shared canvas so they don't overlap
    — each piece is drafted in its own local coordinates starting near
    (0, 0), so without this every piece would land on top of the others."""
    placed = []
    cursor_x = 0.0
    for piece in pieces:
        stitching = piece_outline(piece)
        cut = offset_polyline(stitching, piece.seam_allowance_mm)
        cut_minx, cut_miny, cut_maxx, cut_maxy = bounding_box(cut)
        offset_x = cursor_x - cut_minx
        offset_y = -cut_miny
        placed.append(_Placed(piece, offset_x, offset_y, stitching, cut, polygon_centroid(stitching)))
        cursor_x += (cut_maxx - cut_minx) + _PIECE_GAP_MM
    return placed


def _canvas_bbox(placed: list[_Placed]) -> tuple[float, float, float, float]:
    minx = min(x + pl.offset_x for pl in placed for x, _ in pl.cut)
    maxx = max(x + pl.offset_x for pl in placed for x, _ in pl.cut)
    miny = min(y + pl.offset_y for pl in placed for _, y in pl.cut)
    maxy = max(y + pl.offset_y for pl in placed for _, y in pl.cut)
    return minx, miny, maxx, maxy


def _tile_starts(total_mm: float, usable_mm: float, step_mm: float) -> list[float]:
    if total_mm <= usable_mm:
        return [0.0]
    starts = [0.0]
    while starts[-1] + usable_mm < total_mm:
        starts.append(starts[-1] + step_mm)
    return starts


def _tile_code(row_i: int, col_i: int) -> str:
    """Row letter + column number, e.g. (0, 0) -> 'A1', (1, 2) -> 'B3' —
    printed on each page and in the overview grid so adjacent pages can be
    matched by name instead of by eye."""
    return f"{chr(ord('A') + row_i)}{col_i + 1}"


def _seam_coordinate(window_start: float, usable_mm: float, far_side: bool) -> float:
    """This page's trim-line coordinate, in canvas space, on one edge —
    simply the window's own boundary (window_start, or window_start +
    usable_mm on the far side), with NO additional inset.

    to_pt() already adds margin_mm when it maps a canvas coordinate to
    page-point space (that's what makes window_start itself land exactly
    margin_mm from the physical page edge) — adding margin_mm again here
    would double it, which is exactly a bug this function used to have.

    Because this is just the plain window boundary, it's the SAME on every
    edge of every page, uniformly, whether that edge is shared with a
    neighbour or is an outer boundary.

    For two adjacent pages to agree on one exact coordinate here — needed
    for the half-diamond match marks to land on the same real-world point
    when physically cut and taped — render_pattern_pdf tiles with ZERO
    overlap: page N+1's window starts exactly where page N's ends
    (window_x0_{N+1} = window_x0_N + usable_mm), which makes page N's
    far-side seam and page N+1's near-side seam the literal same value,
    not merely a mathematical approximation of it. See
    test_seam_coordinate_matches_between_adjacent_tiles.
    """
    return (window_start + usable_mm) if far_side else window_start


def _clamp_label_x(label_x: float, text_width: float, right_limit: float, margin: float = 2.0) -> float:
    """Slide a left-anchored text's x position left just far enough that
    `label_x + text_width` stays inside `right_limit` (with a small visual
    margin) — otherwise the clip silently truncates it. Both in point
    space; units don't matter as long as all three args share one."""
    if label_x + text_width > right_limit - margin:
        return right_limit - margin - text_width
    return label_x


def _clamp_label_y_offset(anchor_y: float, desired_offset: float, bottom_limit: float, margin: float = 5.0) -> float:
    """How far below `anchor_y` the label can actually move toward
    `desired_offset` before hitting `bottom_limit` (this page's own bottom
    seam) — never negative, never more room than what's actually left.
    Canvas-mm space, not point space; `desired_offset` is positive
    (downward, since canvas y increases downward)."""
    return min(desired_offset, max(0.0, bottom_limit - anchor_y - margin))


def _draw_polygon(c: canvas.Canvas, points: list[tuple[float, float]]) -> None:
    if len(points) < 2:
        return
    path = c.beginPath()
    path.moveTo(*points[0])
    for x, y in points[1:]:
        path.lineTo(x, y)
    c.drawPath(path, stroke=1, fill=0)


def _draw_cross(c: canvas.Canvas, center_pt: tuple[float, float]) -> None:
    x, y = center_pt
    half = (_REGISTRATION_MARK_MM / 2) * mm
    c.setStrokeColorRGB(0.5, 0.5, 0.5)
    c.setLineWidth(0.25)
    c.line(x - half, y, x + half, y)
    c.line(x, y - half, x, y + half)


def _half_diamond_vertices(
    seam_coord: float, along_coord: float, size_mm: float, vertical: bool, apex_sign: float,
) -> list[tuple[float, float]]:
    """Three canvas-space vertices of half a diamond — a triangle whose
    flat edge sits exactly on the seam (the trim line — see
    _seam_coordinate, now the same exact coordinate for both pages sharing
    an edge, zero overlap) and whose apex points into this page's own
    content, away from the seam. `vertical`: the seam is a vertical line (a
    left/right page edge) if True, horizontal (top/bottom) if False.
    `apex_sign`: +1 or -1 for which side of the seam this page's content is
    on. The complementary page, drawing its own half with the opposite
    apex_sign at the same seam_coord, produces the other half — cut both
    pages along the trim line and tape the two cut edges together, flat
    edges touching, and the two triangles' shared corners (both ends of the
    flat edge) line up, reading as one diamond."""
    r = size_mm / 2
    if vertical:
        flat_a, flat_b = (seam_coord, along_coord - r), (seam_coord, along_coord + r)
        apex = (seam_coord + apex_sign * r, along_coord)
    else:
        flat_a, flat_b = (along_coord - r, seam_coord), (along_coord + r, seam_coord)
        apex = (along_coord, seam_coord + apex_sign * r)
    return [flat_a, apex, flat_b]


def _draw_half_diamond(c: canvas.Canvas, vertices_pt: list[tuple[float, float]]) -> None:
    """Outline only, not filled."""
    path = c.beginPath()
    path.moveTo(*vertices_pt[0])
    for v in vertices_pt[1:]:
        path.lineTo(*v)
    path.close()
    c.setStrokeColorRGB(1, 0, 0)
    c.setLineWidth(0.75)
    c.drawPath(path, stroke=1, fill=0)


def _piece_seam_crossings(
    placed: list[_Placed], seam_coord: float, window_lo: float, window_hi: float, vertical: bool,
) -> list[float]:
    """One position per piece whose cut outline actually spans the seam —
    a vertical line at x=seam_coord if `vertical`, else a horizontal line
    at y=seam_coord — clamped to this page's visible window. A piece that
    doesn't reach this seam at all gets no position back; there's nothing
    there to align, so no mark belongs there. Two pieces crossing the same
    seam at different heights (e.g. front and back) each get their own."""
    positions = []
    for pl in placed:
        minx, miny, maxx, maxy = bounding_box(pl.cut)
        if vertical:
            lo, hi = miny + pl.offset_y, maxy + pl.offset_y
            spans = (minx + pl.offset_x) < seam_coord < (maxx + pl.offset_x)
        else:
            lo, hi = minx + pl.offset_x, maxx + pl.offset_x
            spans = (miny + pl.offset_y) < seam_coord < (maxy + pl.offset_y)
        if not spans:
            continue
        clamped_lo, clamped_hi = max(lo, window_lo), min(hi, window_hi)
        if clamped_lo < clamped_hi:
            positions.append((clamped_lo + clamped_hi) / 2)
    return positions


def _draw_arrowhead(
    c: canvas.Canvas, tip: tuple[float, float], direction: tuple[float, float], size_mm: float,
) -> None:
    """A small filled triangle at `tip`, pointing along unit vector
    `direction`. Both already in point space — fine, since every transform
    this module uses is uniform (no non-isotropic scaling), so angles and
    unit vectors survive it."""
    ux, uy = direction
    px, py = -uy, ux
    size = size_mm * mm
    back_x, back_y = tip[0] - ux * size, tip[1] - uy * size
    left = (back_x + px * size * 0.4, back_y + py * size * 0.4)
    right = (back_x - px * size * 0.4, back_y - py * size * 0.4)
    path = c.beginPath()
    path.moveTo(*tip)
    path.lineTo(*left)
    path.lineTo(*right)
    path.close()
    c.drawPath(path, stroke=0, fill=1)


def _draw_calibration_square(
    c: canvas.Canvas, x_mm: float, y_mm: float, size_mm: float, label: str,
) -> None:
    c.setStrokeColorRGB(0, 0, 0)
    c.setFillColorRGB(0, 0, 0)
    c.setLineWidth(1.0)
    c.rect(x_mm * mm, y_mm * mm, size_mm * mm, size_mm * mm, stroke=1, fill=0)
    c.setFont("Helvetica", 7)
    c.drawString(x_mm * mm, (y_mm + size_mm + 3) * mm, label)


def _draw_page(
    c: canvas.Canvas, placed: list[_Placed], window_x0: float, window_y0: float,
    usable_w_mm: float, usable_h_mm: float, margin_mm: float, page_h_mm: float,
    page_num: int, total_pages: int, tile_code: str,
    tape_left_to: str | None, tape_top_to: str | None,
    tape_right_to: str | None, tape_bottom_to: str | None,
) -> None:
    def to_pt(cx: float, cy: float) -> tuple[float, float]:
        local_x = cx - window_x0 + margin_mm
        local_y = cy - window_y0 + margin_mm
        page_y = page_h_mm - local_y   # pattern y is down; PDF page y is up
        return (local_x * mm, page_y * mm)

    # Seam coordinates computed up front: this page's printable area is
    # exactly its own window, margin-to-margin (see _seam_coordinate) — the
    # SAME uniform inset on every edge, shared with a neighbour or not — so
    # the clip below, and everything drawn inside it, never extends past
    # where the trim line actually is.
    seam_left_x = _seam_coordinate(window_x0, usable_w_mm, far_side=False)
    seam_right_x = _seam_coordinate(window_x0, usable_w_mm, far_side=True)
    seam_top_y = _seam_coordinate(window_y0, usable_h_mm, far_side=False)
    seam_bottom_y = _seam_coordinate(window_y0, usable_h_mm, far_side=True)

    c.saveState()
    clip_path = c.beginPath()
    clip_x0, clip_y0 = to_pt(seam_left_x, seam_bottom_y)
    clip_x1, clip_y1 = to_pt(seam_right_x, seam_top_y)
    clip_path.rect(min(clip_x0, clip_x1), min(clip_y0, clip_y1), abs(clip_x1 - clip_x0), abs(clip_y1 - clip_y0))
    c.clipPath(clip_path, stroke=0, fill=0)

    # Registration grid at fixed canvas positions — clipping/translation
    # means the same mark lands on both sides of an overlap seam, which is
    # part of the alignment reference for taping tiles together.
    gx = _REGISTRATION_SPACING_MM * (window_x0 // _REGISTRATION_SPACING_MM)
    for _ in range(_MAX_GRID_ITERATIONS):
        if gx > window_x0 + usable_w_mm:
            break
        gy = _REGISTRATION_SPACING_MM * (window_y0 // _REGISTRATION_SPACING_MM)
        for _ in range(_MAX_GRID_ITERATIONS):
            if gy > window_y0 + usable_h_mm:
                break
            _draw_cross(c, to_pt(gx, gy))
            gy += _REGISTRATION_SPACING_MM
        gx += _REGISTRATION_SPACING_MM

    # Trim line: grey dashed, deliberately different from both the red
    # seam-allowance dash and the plain registration crosses. Drawn exactly
    # on the clip boundary set above — which is why nothing on this page
    # (pattern lines, labels, crosses) ever prints past it. A tiny inset
    # keeps the dashed hairline itself from being half-clipped by that same
    # boundary (a stroke centred exactly on a clip edge loses its outer
    # half); the pattern content isn't inset, only this cosmetic stroke.
    stroke_inset_mm = 0.3
    c.setStrokeColorRGB(0.65, 0.65, 0.65)
    c.setLineWidth(1.0)
    c.setDash(_TRIM_DASH)
    tx0, ty0 = to_pt(seam_left_x + stroke_inset_mm, seam_top_y + stroke_inset_mm)
    tx1, ty1 = to_pt(seam_right_x - stroke_inset_mm, seam_bottom_y - stroke_inset_mm)
    c.rect(min(tx0, tx1), min(ty0, ty1), abs(tx1 - tx0), abs(ty1 - ty0), stroke=1, fill=0)
    c.setDash([])

    # Red half-diamond (triangle) match marks: one per pattern piece that
    # actually crosses a given trim line, not a generic grid — a trim line
    # with no fabric spanning it has nothing to align, so it gets no mark.
    # Each page draws only the half on its own side of the seam (see
    # _half_diamond_vertices) — cut both pages along the trim line and tape
    # the two cut edges together, and the two halves read as one diamond.
    window_y_hi = window_y0 + usable_h_mm
    window_x_hi = window_x0 + usable_w_mm
    if tape_left_to is not None:
        for gy in _piece_seam_crossings(placed, seam_left_x, window_y0, window_y_hi, vertical=True):
            verts = _half_diamond_vertices(seam_left_x, gy, _DIAMOND_SIZE_MM, True, apex_sign=1)
            _draw_half_diamond(c, [to_pt(x, y) for x, y in verts])
    if tape_right_to is not None:
        for gy in _piece_seam_crossings(placed, seam_right_x, window_y0, window_y_hi, vertical=True):
            verts = _half_diamond_vertices(seam_right_x, gy, _DIAMOND_SIZE_MM, True, apex_sign=-1)
            _draw_half_diamond(c, [to_pt(x, y) for x, y in verts])
    if tape_top_to is not None:
        for gx in _piece_seam_crossings(placed, seam_top_y, window_x0, window_x_hi, vertical=False):
            verts = _half_diamond_vertices(seam_top_y, gx, _DIAMOND_SIZE_MM, False, apex_sign=1)
            _draw_half_diamond(c, [to_pt(x, y) for x, y in verts])
    if tape_bottom_to is not None:
        for gx in _piece_seam_crossings(placed, seam_bottom_y, window_x0, window_x_hi, vertical=False):
            verts = _half_diamond_vertices(seam_bottom_y, gx, _DIAMOND_SIZE_MM, False, apex_sign=-1)
            _draw_half_diamond(c, [to_pt(x, y) for x, y in verts])

    c.setFont("Helvetica", 6)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    if tape_left_to is not None:
        lx, ly = to_pt(seam_left_x, window_y0 + 20)
        c.drawString(lx + 2, ly - 2, f"trim · tapes to {tape_left_to}")
    if tape_right_to is not None:
        rx, ry = to_pt(seam_right_x, window_y0 + 20)
        c.drawRightString(rx - 2, ry - 2, f"trim · tapes to {tape_right_to}")
    if tape_top_to is not None:
        tx, ty = to_pt(window_x0 + 35, seam_top_y)
        c.drawString(tx + 2, ty - 8, f"trim · tapes to {tape_top_to}")
    if tape_bottom_to is not None:
        bx, by = to_pt(window_x0 + 35, seam_bottom_y)
        c.drawString(bx + 2, by + 3, f"trim · tapes to {tape_bottom_to}")

    for pl in placed:
        def shifted(poly):
            return [to_pt(x + pl.offset_x, y + pl.offset_y) for x, y in poly]

        c.setStrokeColorRGB(0, 0, 0)
        c.setLineWidth(0.75)
        _draw_polygon(c, shifted(pl.stitching))

        c.setStrokeColorRGB(0, 0, 0)
        c.setDash(_SEAM_DASH)
        _draw_polygon(c, shifted(pl.cut))
        c.setDash([])

        c.setStrokeColorRGB(0, 0, 0)
        c.setDash(_DART_DASH)
        for a, b in dart_lines(pl.piece):
            (ax, ay), (bx, by) = shifted([a, b])
            c.line(ax, ay, bx, by)
        c.setDash([])

        # Grainline and label both anchor on the piece's stitching-outline
        # centroid (a bounding-box corner isn't safe — a concave edge like
        # the neckline can put that corner outside the piece entirely), and
        # both are drawn only on the one page whose seam-bounded region
        # actually contains that centroid. Without this guard, a piece
        # spanning several tiles would print its full-length grainline
        # fragment-by-fragment across every one of them — the "multiple
        # green lines" problem — and the label-overflow clamp further down
        # would drag a label belonging on a different tile into view here.
        anchor_x, anchor_y = pl.label_anchor
        canvas_anchor_x, canvas_anchor_y = anchor_x + pl.offset_x, anchor_y + pl.offset_y
        anchor_on_this_page = (
            seam_left_x <= canvas_anchor_x <= seam_right_x
            and seam_top_y <= canvas_anchor_y <= seam_bottom_y
        )

        if pl.piece.grainline is not None and anchor_on_this_page:
            pm = pl.piece.point_map()
            g0, g1 = pm[pl.piece.grainline.start], pm[pl.piece.grainline.end]
            dx_c, dy_c = g1.x - g0.x, g1.y - g0.y
            full_len = (dx_c**2 + dy_c**2) ** 0.5
            if full_len > 0:
                # Direction only, from the real grainline — length is fixed
                # and short, a single schematic arrow centred on the piece
                # rather than the true (possibly piece-spanning) line.
                ux_c, uy_c = dx_c / full_len, dy_c / full_len
                half = _GRAINLINE_LENGTH_MM / 2
                gx0, gy0 = to_pt(canvas_anchor_x - ux_c * half, canvas_anchor_y - uy_c * half)
                gx1, gy1 = to_pt(canvas_anchor_x + ux_c * half, canvas_anchor_y + uy_c * half)
                dx, dy = gx1 - gx0, gy1 - gy0
                length = (dx**2 + dy**2) ** 0.5
                ux, uy = dx / length, dy / length
                c.setStrokeColorRGB(0, 0.5, 0)
                c.setFillColorRGB(0, 0.5, 0)
                c.setLineWidth(1.0)
                c.line(gx0, gy0, gx1, gy1)
                _draw_arrowhead(c, (gx0, gy0), (-ux, -uy), _GRAINLINE_ARROW_MM)
                _draw_arrowhead(c, (gx1, gy1), (ux, uy), _GRAINLINE_ARROW_MM)

        if not anchor_on_this_page:
            continue
        # Offset below the grainline arrow so the two don't print on top of
        # each other — but clamped to whatever room this page's own bottom
        # seam actually leaves. A blind fixed offset can push the label
        # past the clip entirely when the centroid sits close to a shared
        # bottom edge, silently dropping it rather than just crowding it.
        label_y_offset = _clamp_label_y_offset(canvas_anchor_y, _LABEL_OFFSET_FROM_GRAINLINE_MM, seam_bottom_y)
        label_x, label_y = to_pt(canvas_anchor_x, canvas_anchor_y + label_y_offset)
        mirror_note = "cut 2, mirrored" if pl.piece.cut_mirrored else "cut on fold"
        title = f"{pl.piece.name} — cut {pl.piece.cut_count} ({mirror_note})"
        subtitle = f"Seam allowance {pl.piece.seam_allowance_mm:.0f}mm — included in cut line"

        # The clip (now at the seam boundary, not the full margin — see
        # above) can sit closer to the centroid than it used to, and a
        # drawString anchor is its LEFT edge: long text can run past the
        # clip's right edge and get silently truncated. Measure the actual
        # rendered width and slide the anchor left far enough that it never
        # does, rather than guessing at a smaller font and hoping.
        widest = max(c.stringWidth(title, "Helvetica-Bold", 8), c.stringWidth(subtitle, "Helvetica", 7))
        label_x = _clamp_label_x(label_x, widest, max(clip_x0, clip_x1))

        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(label_x, label_y, title)
        c.setFont("Helvetica", 7)
        c.drawString(label_x, label_y - 10, subtitle)

    c.restoreState()

    c.setFont("Helvetica-Bold", 10)
    c.setFillColorRGB(0, 0, 0)
    c.drawRightString((margin_mm + usable_w_mm) * mm, (margin_mm + usable_h_mm - 8) * mm, tile_code)

    c.setFont("Helvetica", 7)
    c.drawString(margin_mm * mm, (margin_mm / 2) * mm, f"Page {page_num} of {total_pages}")


def _draw_overview_page(
    c: canvas.Canvas, placed: list[_Placed], canvas_minx: float, canvas_miny: float,
    canvas_w: float, canvas_h: float, page_w_mm: float, page_h_mm: float, margin_mm: float,
    col_starts: list[float], row_starts: list[float], usable_w_mm: float, usable_h_mm: float,
    total_pages: int,
) -> None:
    """Page 1: a small, scaled-down view of the whole assembled pattern with
    a lettered/numbered grid (matching each tile's own printed code) showing
    which tile goes where — NOT at physical scale. The mandatory 50mm
    calibration square, plus a 1 inch and a 1cm square, are drawn separately
    at true scale in the corner, so "page 1 has real calibration squares"
    stays true even though the pattern view around them isn't to scale.
    """
    overview_w = page_w_mm - 2 * margin_mm
    overview_h = page_h_mm - 2 * margin_mm - _OVERVIEW_HEADING_MM - _OVERVIEW_FOOTER_MM
    scale = min(overview_w / canvas_w, overview_h / canvas_h) if canvas_w and canvas_h else 1.0

    origin_x_mm = margin_mm
    top_y_mm = page_h_mm - margin_mm - _OVERVIEW_HEADING_MM

    def to_pt(cx: float, cy: float) -> tuple[float, float]:
        local_x = (cx - canvas_minx) * scale
        local_y = (cy - canvas_miny) * scale
        return ((origin_x_mm + local_x) * mm, (top_y_mm - local_y) * mm)

    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(margin_mm * mm, (page_h_mm - margin_mm - 8) * mm,
                 "Assembly overview — NOT TO SCALE, reference only")

    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.5)
    for pl in placed:
        poly = [to_pt(x + pl.offset_x, y + pl.offset_y) for x, y in pl.stitching]
        _draw_polygon(c, poly)

        c.setDash(_DART_DASH)
        for a, b in dart_lines(pl.piece):
            (ax, ay) = to_pt(a[0] + pl.offset_x, a[1] + pl.offset_y)
            (bx, by) = to_pt(b[0] + pl.offset_x, b[1] + pl.offset_y)
            c.line(ax, ay, bx, by)
        c.setDash([])

        label = "FRONT" if "front" in pl.piece.id else "BACK" if "back" in pl.piece.id else pl.piece.name.upper()
        label_x, label_y = to_pt(pl.label_anchor[0] + pl.offset_x, pl.label_anchor[1] + pl.offset_y)
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0, 0, 0)
        c.drawCentredString(label_x, label_y, label)

    c.setFont("Helvetica", 9)
    for row_i, wy0 in enumerate(row_starts):
        for col_i, wx0 in enumerate(col_starts):
            x0, y0 = to_pt(wx0, wy0)
            x1, y1 = to_pt(wx0 + usable_w_mm, wy0 + usable_h_mm)
            c.setStrokeColorRGB(0.3, 0.3, 0.9)
            c.setDash([3, 2])
            c.rect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0), stroke=1, fill=0)
            c.setDash([])
            c.setFillColorRGB(0.3, 0.3, 0.9)
            c.drawCentredString((x0 + x1) / 2, (y0 + y1) / 2, _tile_code(row_i, col_i))

    squares = [
        (margin_mm, _CALIBRATION_SQUARE_MM, "50mm square"),
        (margin_mm + 65.0, _CALIBRATION_INCH_MM, "1 inch square"),
        (margin_mm + 105.4, _CALIBRATION_CM_MM, "1cm square"),
    ]
    for x_mm, size_mm, label in squares:
        _draw_calibration_square(c, x_mm, margin_mm, size_mm, label)

    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(
        margin_mm * mm, (margin_mm + _CALIBRATION_SQUARE_MM + 14) * mm,
        "Print at 100% / actual size — all three squares must measure exactly before trusting this PDF.",
    )

    c.setFont("Helvetica", 7)
    c.drawString(margin_mm * mm, (margin_mm / 2) * mm, f"Page 1 of {total_pages}")


def _draw_cut_on_fold_preview_page(
    c: canvas.Canvas, pieces: list[PatternPiece], page_w_mm: float, page_h_mm: float,
    margin_mm: float, page_num: int, total_pages: int,
) -> None:
    """Page 2: each cut-on-fold piece shown FULL (mirrored across its own
    centre front/back), not as the half the rest of this PDF works in —
    NOT TO SCALE, reference only, same as page 1.

    This exists because a half-piece view can look completely fine while
    hiding a real defect: a curve meeting the fold line with anything but
    a horizontal tangent there mirrors into a visible cusp, invisible
    until the two halves are actually joined. See
    `api.export.geometry.mirrored_outline` and CLAUDE.md. Showing this on
    every generated PDF — not just checking it once during development —
    is the point: it's the same view whoever opens this file would need
    to catch that defect themselves.
    """
    foldable = [p for p in pieces if p.cut_mirrored]
    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(margin_mm * mm, (page_h_mm - margin_mm - 8) * mm,
                 "Cut-on-fold preview — NOT TO SCALE, reference only")

    if not foldable:
        c.setFont("Helvetica", 7)
        c.drawString(margin_mm * mm, (margin_mm / 2) * mm, f"Page {page_num} of {total_pages}")
        return

    area_w = page_w_mm - 2 * margin_mm
    area_h = page_h_mm - 2 * margin_mm - _OVERVIEW_HEADING_MM
    cell_w = area_w / len(foldable)

    outlines = [mirrored_outline(p) for p in foldable]
    scale = min(
        (cell_w - 10.0) / max(bounding_box(o)[2] - bounding_box(o)[0] for o in outlines),
        (area_h - 20.0) / max(bounding_box(o)[3] - bounding_box(o)[1] for o in outlines),
    )

    for i, (piece, outline) in enumerate(zip(foldable, outlines)):
        minx, miny, maxx, maxy = bounding_box(outline)
        w, h = maxx - minx, maxy - miny
        cell_x0 = margin_mm + i * cell_w
        origin_x = cell_x0 + (cell_w - w * scale) / 2
        top_y = page_h_mm - margin_mm - _OVERVIEW_HEADING_MM

        def to_pt(px: float, py: float, _minx=minx, _miny=miny, _origin_x=origin_x, _top_y=top_y) -> tuple[float, float]:
            return ((_origin_x + (px - _minx) * scale) * mm, (_top_y - (py - _miny) * scale) * mm)

        c.setStrokeColorRGB(0, 0, 0)
        c.setLineWidth(0.6)
        _draw_polygon(c, [to_pt(x, y) for x, y in outline])

        c.setDash(_DART_DASH)
        for a, b in dart_lines(piece):
            c.line(*to_pt(*a), *to_pt(*b))
            c.line(*to_pt(-a[0], a[1]), *to_pt(-b[0], b[1]))
        c.setDash([])

        label = "FRONT" if "front" in piece.id else "BACK" if "back" in piece.id else piece.name.upper()
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString((cell_x0 + cell_w / 2) * mm, top_y * mm - 14, label)

    c.setFont("Helvetica", 7)
    c.drawString(margin_mm * mm, (margin_mm / 2) * mm, f"Page {page_num} of {total_pages}")


def render_pattern_pdf(
    pieces: list[PatternPiece],
    page_size: str = "letter",
    margin_mm: float = _DEFAULT_MARGIN_MM,
) -> bytes:
    """Tile `pieces` across Letter or A4 pages at true physical scale.
    Returns raw PDF bytes.

    Tiles are laid out with ZERO overlap: each page's window starts
    exactly where the previous one's ends. That isn't a simplification —
    it's required for the half-diamond match marks to work at all. With
    any overlap, page N's own trim line and page N+1's own trim line are
    two different canvas coordinates that merely sit close together; with
    zero overlap, they're the literal same coordinate, so a mark centred
    there and cut in half by the actual paper cut reconstructs exactly.
    """
    if page_size not in _PAGE_SIZES:
        raise ValueError(f"unknown page_size {page_size!r}; expected one of {sorted(_PAGE_SIZES)}")
    page_w_pt, page_h_pt = _PAGE_SIZES[page_size]
    page_w_mm, page_h_mm = page_w_pt / mm, page_h_pt / mm
    usable_w_mm = page_w_mm - 2 * margin_mm
    usable_h_mm = page_h_mm - 2 * margin_mm
    step_w_mm = usable_w_mm
    step_h_mm = usable_h_mm

    placed = _layout_pieces(pieces)
    minx, miny, maxx, maxy = _canvas_bbox(placed)
    canvas_w, canvas_h = maxx - minx, maxy - miny

    col_starts = _tile_starts(canvas_w, usable_w_mm, step_w_mm)
    row_starts = _tile_starts(canvas_h, usable_h_mm, step_h_mm)
    tile_count = len(col_starts) * len(row_starts)
    has_fold_preview = any(p.cut_mirrored for p in pieces)
    total_pages = tile_count + 1 + (1 if has_fold_preview else 0)  # overview [+ fold preview] + tiles

    buf = BytesIO()
    # pageCompression=0: keep content streams uncompressed. Slightly larger
    # files at this scale don't matter; being able to grep the PDF bytes
    # (for tests, and for debugging a bad print) does.
    c = canvas.Canvas(buf, pagesize=(page_w_pt, page_h_pt), pageCompression=0)

    _draw_overview_page(
        c, placed, minx, miny, canvas_w, canvas_h, page_w_mm, page_h_mm, margin_mm,
        col_starts, row_starts, usable_w_mm, usable_h_mm, total_pages,
    )
    c.showPage()

    page_num = 1
    if has_fold_preview:
        page_num += 1
        _draw_cut_on_fold_preview_page(c, pieces, page_w_mm, page_h_mm, margin_mm, page_num, total_pages)
        c.showPage()

    for row_i, wy0 in enumerate(row_starts):
        for col_i, wx0 in enumerate(col_starts):
            page_num += 1
            tile_code = _tile_code(row_i, col_i)
            tape_left_to = _tile_code(row_i, col_i - 1) if col_i > 0 else None
            tape_top_to = _tile_code(row_i - 1, col_i) if row_i > 0 else None
            tape_right_to = _tile_code(row_i, col_i + 1) if col_i < len(col_starts) - 1 else None
            tape_bottom_to = _tile_code(row_i + 1, col_i) if row_i < len(row_starts) - 1 else None
            _draw_page(
                c, placed, minx + wx0, miny + wy0, usable_w_mm, usable_h_mm,
                margin_mm, page_h_mm, page_num, total_pages, tile_code,
                tape_left_to, tape_top_to, tape_right_to, tape_bottom_to,
            )
            c.showPage()

    c.save()
    return buf.getvalue()
