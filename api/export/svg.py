"""PatternPiece geometry -> standalone SVG, in real millimetre units (1 user
unit == 1mm) so the output is physically scaled wherever it's rendered or
re-used (e.g. the Phase 7 pattern viewer). Renders the stitching line, the
cut line (seam allowance offset outward), darts, notches, grainline, and a
piece label with cut count / mirroring.
"""
from __future__ import annotations

import svgwrite

from api.export.geometry import (
    bounding_box, dart_lines, offset_polyline, piece_outline, polygon_centroid,
)
from api.schema import PatternPiece

_MARGIN_MM = 20.0
_NOTCH_LENGTH_MM = 6.0
_GRAINLINE_ARROW_MM = 4.0


def _notch_marks(piece: PatternPiece) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """A short horizontal tick mark at each notch's point. Approximate — a
    real perpendicular-to-the-outline tick is a later refinement, fine for
    a first-draft marking."""
    pm = piece.point_map()
    marks = []
    for notch in piece.notches:
        p = pm[notch.point]
        marks.append(((p.x - _NOTCH_LENGTH_MM / 2, p.y), (p.x + _NOTCH_LENGTH_MM / 2, p.y)))
    return marks


def piece_to_svg(piece: PatternPiece) -> str:
    stitching = piece_outline(piece)
    cut = offset_polyline(stitching, piece.seam_allowance_mm)
    minx, miny, maxx, maxy = bounding_box(cut)

    width = (maxx - minx) + 2 * _MARGIN_MM
    height = (maxy - miny) + 2 * _MARGIN_MM
    ox, oy = minx - _MARGIN_MM, miny - _MARGIN_MM

    def shift(poly):
        return [(x - ox, y - oy) for x, y in poly]

    dwg = svgwrite.Drawing(size=(f"{width:.2f}mm", f"{height:.2f}mm"), viewBox=f"0 0 {width:.2f} {height:.2f}")

    dwg.add(dwg.polygon(points=shift(cut), fill="none", stroke="black", stroke_width=0.5, stroke_dasharray="4,2"))
    dwg.add(dwg.polygon(points=shift(stitching), fill="none", stroke="black", stroke_width=0.75))

    for (a, b) in dart_lines(piece):
        (ax, ay), (bx, by) = shift([a])[0], shift([b])[0]
        dwg.add(dwg.line(start=(ax, ay), end=(bx, by), stroke="black", stroke_width=0.5, stroke_dasharray="2,1"))

    for (a, b) in _notch_marks(piece):
        (ax, ay), (bx, by) = shift([a])[0], shift([b])[0]
        dwg.add(dwg.line(start=(ax, ay), end=(bx, by), stroke="blue", stroke_width=0.5))

    if piece.grainline is not None:
        pm = piece.point_map()
        g0, g1 = pm[piece.grainline.start], pm[piece.grainline.end]
        (gx0, gy0), (gx1, gy1) = shift([(g0.x, g0.y), (g1.x, g1.y)])
        dwg.add(dwg.line(start=(gx0, gy0), end=(gx1, gy1), stroke="green", stroke_width=0.75))
        dx, dy = gx1 - gx0, gy1 - gy0
        length = (dx**2 + dy**2) ** 0.5
        if length > 0:
            ux, uy = dx / length, dy / length
            dwg.add(dwg.polygon(points=_arrowhead((gx0, gy0), (-ux, -uy)), fill="green"))
            dwg.add(dwg.polygon(points=_arrowhead((gx1, gy1), (ux, uy)), fill="green"))

    # Label sits at the stitching outline's own centroid — a bounding-box
    # corner isn't safe here, a concave edge (the neckline curve) can put
    # that corner in empty space outside the piece entirely.
    label_x, label_y = shift([polygon_centroid(stitching)])[0]
    mirror_note = "cut 2, mirrored" if piece.cut_mirrored else "cut on fold"
    dwg.add(dwg.text(
        f"{piece.name} — cut {piece.cut_count} ({mirror_note})",
        insert=(label_x, label_y), font_size="4px", font_weight="bold",
    ))
    dwg.add(dwg.text(
        f"Seam allowance {piece.seam_allowance_mm:.0f}mm — included in cut line",
        insert=(label_x, label_y + 5), font_size="3.5px",
    ))

    return dwg.tostring()


def _arrowhead(tip: tuple[float, float], direction: tuple[float, float], size_mm: float = _GRAINLINE_ARROW_MM):
    ux, uy = direction
    px, py = -uy, ux
    back = (tip[0] - ux * size_mm, tip[1] - uy * size_mm)
    left = (back[0] + px * size_mm * 0.4, back[1] + py * size_mm * 0.4)
    right = (back[0] - px * size_mm * 0.4, back[1] - py * size_mm * 0.4)
    return [tip, left, right]
