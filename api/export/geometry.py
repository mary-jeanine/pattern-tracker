"""Pure geometry helpers shared by svg.py and pdf.py: turn a PatternPiece's
named points/segments into flat (x, y) polylines, and offset a polyline
outward for the seam allowance.

Curve offsetting is genuinely fiddly (see docs/PLAN.md Phase 3 note): a
cubic bezier can't be offset exactly in closed form. This flattens to a
polyline first and offsets *that*, per-vertex, with a miter join — an
accepted approximation, not an exact curve offset.

Outline reconstruction assumes a piece's edges plus its darts' legs form a
single simple closed loop (every point has exactly two connections) — true
for everything api/drafting currently generates. A piece with branching
geometry (a dart that isn't edge-attached, an internal seam) isn't handled.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from api.schema import PatternPiece, Point, Segment, SegmentKind


def _bezier_point(
    p0: tuple[float, float], c1: tuple[float, float], c2: tuple[float, float],
    p3: tuple[float, float], t: float,
) -> tuple[float, float]:
    mt = 1 - t
    x = mt**3 * p0[0] + 3 * mt**2 * t * c1[0] + 3 * mt * t**2 * c2[0] + t**3 * p3[0]
    y = mt**3 * p0[1] + 3 * mt**2 * t * c1[1] + 3 * mt * t**2 * c2[1] + t**3 * p3[1]
    return (x, y)


def flatten_segment(
    points: dict[str, Point], seg: Segment, steps: int = 24,
) -> list[tuple[float, float]]:
    """Sample a Segment start -> end, inclusive. Straight line for LINE
    segments; `steps` + 1 points along the curve for CUBIC."""
    p0 = points[seg.start]
    p3 = points[seg.end]
    if seg.kind != SegmentKind.CUBIC or seg.c1 is None or seg.c2 is None:
        return [(p0.x, p0.y), (p3.x, p3.y)]
    return [
        _bezier_point((p0.x, p0.y), seg.c1, seg.c2, (p3.x, p3.y), i / steps)
        for i in range(steps + 1)
    ]


def piece_outline(piece: PatternPiece, steps: int = 24) -> list[tuple[float, float]]:
    """Reconstruct the piece's closed stitching-line outline as one polyline
    (first point == last point), by chaining edge segments through shared
    point ids. A dart's two legs are connected by a straight synthetic edge
    for this purpose — the dart itself is drawn separately as an annotation
    (see dart_lines()), not cut as a V into the outline; see module
    docstring for the "single simple closed loop" assumption this relies
    on.

    That synthetic leg_a<->leg_b edge is only added when at least one leg
    still needs it to reach degree 2, i.e. it has no real edge path of its
    own yet. Some darts route an actual point between their legs (see
    bodice.py's "point out" construction, e.g. a dart's D1) as real edge
    segments instead — there, both legs already have two real connections,
    and adding the synthetic one too would give them three, breaking the
    "simple closed loop" assumption.
    """
    pm = piece.point_map()
    adjacency: dict[str, list[tuple[str, Optional[Segment]]]] = defaultdict(list)
    for edge in piece.edges:
        for seg in edge.segments:
            adjacency[seg.start].append((seg.end, seg))
            adjacency[seg.end].append((seg.start, seg))
    for dart in piece.darts:
        if len(adjacency[dart.leg_a]) < 2 and len(adjacency[dart.leg_b]) < 2:
            adjacency[dart.leg_a].append((dart.leg_b, None))
            adjacency[dart.leg_b].append((dart.leg_a, None))

    if not adjacency:
        return []

    start = next(iter(adjacency))
    outline: list[tuple[float, float]] = [(pm[start].x, pm[start].y)]
    visited: set[tuple[str, str, int]] = set()
    current = start
    while True:
        candidates = [
            (nxt, seg) for nxt, seg in adjacency[current]
            if (current, nxt, id(seg)) not in visited
        ]
        if not candidates:
            break
        nxt, seg = candidates[0]
        visited.add((current, nxt, id(seg)))
        visited.add((nxt, current, id(seg)))
        if seg is None:
            outline.append((pm[nxt].x, pm[nxt].y))
        else:
            pts = flatten_segment(pm, seg, steps)
            if seg.start != current:
                pts = list(reversed(pts))
            outline.extend(pts[1:])
        current = nxt
        if current == start:
            break
    return outline


def mirrored_outline(piece: PatternPiece, steps: int = 24) -> list[tuple[float, float]]:
    """The piece's FULL, cut-on-the-fold shape: `piece_outline()` mirrored
    across its own fold line (x=0 in the piece's local coordinates —
    centre back or centre front) and joined into one closed polygon.

    This exists because a half-piece view can hide a real defect
    completely. A curve touching the fold line only mirrors into a smooth
    continuation if its tangent there is horizontal (perpendicular to the
    fold); get that wrong and the half-piece still looks fine on its own
    — the problem only appears once you see both halves joined, as a
    visible cusp or pinch exactly on the fold line. (Found on this
    project's bodice neckline: looked correct as a half-piece, showed a
    sharp double-bump at centre back once actually mirrored and
    rendered.) Always check this view, not just the half-piece, after any
    change to geometry that touches a fold-line edge (centre front/back)
    — see CLAUDE.md.

    Raises ValueError if the outline doesn't have exactly two points on
    the fold line (x=0) — every piece this project drafts is cut on the
    fold at both its top and bottom (e.g. neckline-side and waist-side),
    so anything else means the piece isn't actually fold-symmetric and
    mirroring it would silently produce a meaningless shape.
    """
    outline = piece_outline(piece, steps)
    pts = outline[:-1] if outline and outline[0] == outline[-1] else outline[:]
    fold_idx = [i for i, (x, _y) in enumerate(pts) if abs(x) < 1e-6]
    if len(fold_idx) != 2:
        raise ValueError(
            f"{piece.id}: expected exactly 2 points on the fold line (x=0), found {len(fold_idx)} "
            "— this piece may not be a simple cut-on-fold half, or a point that should sit exactly "
            "on the fold is off by a rounding error."
        )
    # Rotated so pts[0] is one fold point (A) and pts[-1] is the other (B)
    # — piece_outline is a cycle, and the wrap-around segment pts[-1] back
    # to pts[0] is exactly the centre-front/back edge, so pts already IS
    # the outer boundary from A to B with nothing further to trim.
    pts = pts[fold_idx[0]:] + pts[:fold_idx[0]]
    mirrored = [(-x, y) for x, y in pts]
    # Append the mirrored outer boundary walked backward (B -> ... -> A),
    # dropping its first point (mirror(B) == B == pts[-1], or it would
    # repeat as a zero-length segment) and its last (mirror(A) == A ==
    # pts[0], but possibly -0.0 instead of 0.0 — the implicit closing
    # wrap already returns to the exact pts[0] object, so this drops that
    # near-duplicate rather than let a sign-of-zero mismatch register as
    # a spurious self-intersection on the closing segment).
    full = pts + list(reversed(mirrored))[1:-1]
    full.append(full[0])
    return full


def signed_area(polygon: list[tuple[float, float]]) -> float:
    """Shoelace formula. Sign (not magnitude) tells offset_polyline which
    way is outward; magnitude is only meaningful if the polygon is closed."""
    pts = polygon[:-1] if polygon and polygon[0] == polygon[-1] else polygon
    n = len(pts)
    area = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return area / 2


def bounding_box(polyline: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in polyline]
    ys = [p[1] for p in polyline]
    return (min(xs), min(ys), max(xs), max(ys))


def polygon_centroid(polygon: list[tuple[float, float]]) -> tuple[float, float]:
    """Area-weighted centroid of a closed polygon. A bounding-box corner is
    NOT a safe stand-in for "a point inside the shape" — a concave edge
    (e.g. a neckline curve) can put that corner in empty space outside the
    piece entirely. The centroid is far more reliably interior for a
    garment-piece-shaped polygon, though still not geometrically guaranteed
    for an arbitrarily concave shape; see point_in_polygon() to verify."""
    pts = polygon[:-1] if polygon and polygon[0] == polygon[-1] else polygon
    area = 0.0
    cx = 0.0
    cy = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    area *= 0.5
    if abs(area) < 1e-9:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    return (cx / (6 * area), cy / (6 * area))


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Standard ray-casting point-in-polygon test."""
    x, y = point
    pts = polygon[:-1] if polygon and polygon[0] == polygon[-1] else polygon
    inside = False
    x0, y0 = pts[-1]
    for x1, y1 in pts:
        if (y1 > y) != (y0 > y):
            x_at_y = (x0 - x1) * (y - y1) / (y0 - y1) + x1
            if x < x_at_y:
                inside = not inside
        x0, y0 = x1, y1
    return inside


def _normalize(dx: float, dy: float) -> tuple[float, float]:
    length = (dx**2 + dy**2) ** 0.5
    if length == 0:
        return (0.0, 0.0)
    return (dx / length, dy / length)


def _segment_intersection(
    p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float], p4: tuple[float, float],
) -> Optional[tuple[float, float]]:
    x1, y1 = p1; x2, y2 = p2; x3, y3 = p3; x4, y4 = p4
    d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(d) < 1e-12:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / d
    u = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / d
    if 0 <= t <= 1 and 0 <= u <= 1:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None


def _remove_self_intersections(
    pts: list[tuple[float, float]], max_iters: int = 50,
) -> list[tuple[float, float]]:
    """Offsetting a polyline outward is only exact for a convex region — a
    concave stretch tighter than the offset distance folds the naive
    per-vertex offset over on itself, producing a small self-intersecting
    loop (found on a real bodice armhole: a tight concave curve, well
    within normal range, offset by a standard 15mm seam allowance). A cut
    line like that isn't just a rendering glitch, it isn't actually
    cuttable either. This finds each crossing pair of non-adjacent
    segments and splices out the loop between them, replacing it with
    their actual intersection point, until none remain (or the iteration
    cap is hit, as a safety bound — this is a local cleanup, not a general
    polygon-boolean library, so a pathological input could in principle
    still have a residual crossing)."""
    pts = pts[:]
    for _ in range(max_iters):
        n = len(pts)
        resolved = False
        for i in range(n):
            a1, a2 = pts[i], pts[(i + 1) % n]
            for j in range(i + 2, n):
                if i == 0 and j == n - 1:
                    continue  # adjacent, wrapping around
                b1, b2 = pts[j], pts[(j + 1) % n]
                ip = _segment_intersection(a1, a2, b1, b2)
                if ip is not None:
                    pts = pts[: i + 1] + [ip] + pts[j + 1 :]
                    resolved = True
                    break
            if resolved:
                break
        if not resolved:
            break
    return pts


def offset_polyline(polygon: list[tuple[float, float]], distance: float) -> list[tuple[float, float]]:
    """Offset a closed polygon outward by `distance` mm, per-vertex, with a
    mitered join clamped to 4x the offset distance to avoid spikes at sharp
    corners. Approximate (see module docstring) — good enough for a cut
    line, not an exact curve offset. Self-intersecting loops this can
    introduce on a tight concave stretch are cleaned up afterward (see
    `_remove_self_intersections`)."""
    pts = polygon[:-1] if polygon and polygon[0] == polygon[-1] else polygon[:]
    n = len(pts)
    if n < 3:
        return polygon[:]

    sign = 1.0 if signed_area(pts) < 0 else -1.0
    out: list[tuple[float, float]] = []
    for i in range(n):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        d1 = _normalize(x1 - x0, y1 - y0)
        d2 = _normalize(x2 - x1, y2 - y1)
        n1 = (-d1[1] * sign, d1[0] * sign)
        n2 = (-d2[1] * sign, d2[0] * sign)
        bis = _normalize(n1[0] + n2[0], n1[1] + n2[1])
        cos_half = n1[0] * bis[0] + n1[1] * bis[1]
        miter = distance / cos_half if abs(cos_half) > 1e-6 else distance
        miter = max(min(miter, distance * 4), -distance * 4)
        out.append((x1 + bis[0] * miter, y1 + bis[1] * miter))

    out = _remove_self_intersections(out)
    out.append(out[0])
    return out


def dart_lines(piece: PatternPiece) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Two line segments per dart (leg_a -> apex, apex -> leg_b), for drawing
    as an internal annotation."""
    pm = piece.point_map()
    lines = []
    for dart in piece.darts:
        apex, a, b = pm[dart.apex], pm[dart.leg_a], pm[dart.leg_b]
        lines.append(((a.x, a.y), (apex.x, apex.y)))
        lines.append(((apex.x, apex.y), (b.x, b.y)))
    return lines
