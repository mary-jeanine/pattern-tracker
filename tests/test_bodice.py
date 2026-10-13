"""Relationship tests for api.drafting.bodice, written against the real
"How to Draft a Bodice Block" (inthefolds.com) construction this module
follows — see its module docstring for exactly which steps are faithful
and which are documented judgment calls. Per CLAUDE.md / docs/PLAN.md,
these check *relationships* derived from the input measurements, not
magic output numbers.

The primary fixture uses a real set of body measurements (converted from
inches) rather than a guessed "standard size", since this module was
rebuilt specifically against them.
"""
import math

from pytest import approx as pytest_approx

import dataclasses

from api.drafting.bodice import _BACK_WIDTH_EASE_CM, _shared_frame, draft_bodice, draft_bodice_from_sloper
from api.schema import DesignIntent, FabricClass, GarmentType, Measurements, PatternPiece, SegmentKind

_IN_TO_MM = 25.4
# bodice.py works internally in cm (per the user-supplied construction spec);
# this test file checks stored (schema) values, which are mm — so cm-based
# constants imported from bodice.py need converting here.
_BACK_WIDTH_EASE_MM = _BACK_WIDTH_EASE_CM * 10


# --------------------------------------------------------------------------
# Fixtures / geometry helpers
# --------------------------------------------------------------------------
def _measurements(**overrides) -> Measurements:
    defaults = dict(
        bust=34 * _IN_TO_MM,
        waist=31.5 * _IN_TO_MM,
        hip=38 * _IN_TO_MM,                 # not used by the bodice draft; a plausible fill-in
        shoulder_width=15 * _IN_TO_MM,       # not used by the bodice draft; a plausible fill-in
        back_length=14 * _IN_TO_MM,          # nape to waist
        bust_height=11 * _IN_TO_MM,          # shoulder to bust (captured, unused — see module docstring)
        bust_span=7.5 * _IN_TO_MM,           # bust point to point
        armscye_depth=7.5 * _IN_TO_MM,
        nape_to_bust=11 * _IN_TO_MM,
        neck_circumference=15 * _IN_TO_MM,
        shoulder_length=5 * _IN_TO_MM,
        back_width=13.0 * _IN_TO_MM,         # FULL across-back; the draft halves it
    )
    defaults.update(overrides)
    return Measurements(**defaults)


def _intent(**overrides) -> DesignIntent:
    defaults = dict(garment_type=GarmentType.BODICE, fabric_class=FabricClass.WOVEN)
    defaults.update(overrides)
    return DesignIntent(**defaults)


def _piece(pieces: list[PatternPiece], piece_id: str) -> PatternPiece:
    return next(p for p in pieces if p.id == piece_id)


def _edge(piece: PatternPiece, edge_id: str):
    return next(e for e in piece.edges if e.id == edge_id)


def _bezier_point(p0, c1, c2, p3, t):
    mt = 1 - t
    x = mt**3 * p0[0] + 3 * mt**2 * t * c1[0] + 3 * mt * t**2 * c2[0] + t**3 * p3[0]
    y = mt**3 * p0[1] + 3 * mt**2 * t * c1[1] + 3 * mt * t**2 * c2[1] + t**3 * p3[1]
    return (x, y)


def _sample_segment(points: dict, seg, steps: int = 40) -> list[tuple[float, float]]:
    p0 = points[seg.start]
    p3 = points[seg.end]
    if seg.kind != SegmentKind.CUBIC:
        return [(p0.x, p0.y), (p3.x, p3.y)]
    return [
        _bezier_point((p0.x, p0.y), seg.c1, seg.c2, (p3.x, p3.y), i / steps)
        for i in range(steps + 1)
    ]


def _edge_length(piece: PatternPiece, edge_id: str) -> float:
    pm = piece.point_map()
    total = 0.0
    for seg in _edge(piece, edge_id).segments:
        poly = _sample_segment(pm, seg)
        for a, b in zip(poly, poly[1:]):
            total += ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
    return total


def _dist(pm, id_a, id_b) -> float:
    a, b = pm[id_a], pm[id_b]
    return math.hypot(a.x - b.x, a.y - b.y)


def _max_perpendicular_deviation(poly: list[tuple[float, float]]) -> float:
    """How far the curve bulges off its own straight start->end chord —
    used to check the armhole is a modest curve, not a straight line and
    not a wild bulge."""
    x0, y0 = poly[0]
    x1, y1 = poly[-1]
    dx, dy = x1 - x0, y1 - y0
    chord = math.hypot(dx, dy)
    best = 0.0
    for x, y in poly:
        # perpendicular distance from (x,y) to the line through (x0,y0)-(x1,y1)
        d = abs((x - x0) * dy - (y - y0) * dx) / chord
        best = max(best, d)
    return best


# --------------------------------------------------------------------------
# Required relationships (docs/PLAN.md Phase 2)
# --------------------------------------------------------------------------
def test_front_and_back_armholes_are_comparable_in_length():
    """Which of front/back is longer has flipped twice now as the back
    shoulder tip (O)'s construction changed — docs/PLAN.md originally
    guessed front > back; the old pivot-based O produced back > front;
    the user's current direct formula for O produces front > back again,
    closely balanced (~5% apart for a real test body). None of these had
    strong grounding, so this no longer asserts a direction — just that
    the two stay in the same ballpark, which a construction error (e.g.
    one armhole much too short) would violate."""
    pieces = draft_bodice(_measurements(), _intent())
    front = _piece(pieces, "bodice_front")
    back = _piece(pieces, "bodice_back")

    front_len = _edge_length(front, "armhole")
    back_len = _edge_length(back, "armhole")
    ratio = max(front_len, back_len) / min(front_len, back_len)
    assert ratio < 1.5, f"front={front_len:.1f}mm back={back_len:.1f}mm look too far apart"


def test_total_waist_width_equals_waist_plus_bust_and_waist_ease():
    """The pattern rectangle is drawn at a constant (bust+ease_bust_mm)/2
    half-width from the bustline down to the waistline (step 6), and the
    dart-width calc (step 42) removes bust-(waist+ease_waist_mm) worth of
    width (half the body, split three ways) — using RAW bust, not
    bust+ease_bust_mm, since the rectangle's own extra bust ease was never
    separately added to the waist suppression. Algebraically that means
    the final waist width isn't simply waist+ease_waist_mm (the first
    version of this module's identity) — the bust ease "shows up" at the
    waist too, since nothing in the construction removes it there. See
    the module docstring's note on step 42.

    Each dart's width is measured as the horizontal (x) span between its
    legs, not the straight-line distance between them: since step 47
    (`_bf_y`) the waist dart legs sit on the sloped, balanced B-F line
    rather than a flat one, so a dart's two legs are no longer at the
    same y -- the straight-line leg-to-leg distance is a hair longer
    than the horizontal width it actually removes from the waistline.

    The side seam's share of the suppression isn't a dart at all (it's
    cut off directly -- see _draft_back/_draft_front), so it's measured
    as the plain inset from the underarm point to the side-waist corner
    instead of a dart's leg-to-leg width.
    """
    m = _measurements()
    intent = _intent()
    pieces = draft_bodice(m, intent)
    front = _piece(pieces, "bodice_front")
    back = _piece(pieces, "bodice_back")

    front_pm, back_pm = front.point_map(), back.point_map()

    def _dart_width_x(darts, pm, dart_id):
        dart = next(d for d in darts if d.id == dart_id)
        return abs(pm[dart.leg_a].x - pm[dart.leg_b].x)

    one_side_flat = (
        (back_pm["underarm_back"].x - back_pm["cb_waist"].x)
        + (front_pm["underarm_front"].x - front_pm["cf_waist"].x)
    )
    one_side_after_darts = (
        one_side_flat
        - _dart_width_x(back.darts, back_pm, "back_waist_dart")
        - _dart_width_x(front.darts, front_pm, "bust_dart")
        - (back_pm["underarm_back"].x - back_pm["side_waist_back"].x)
        - (front_pm["underarm_front"].x - front_pm["side_waist_front"].x)
    )
    total_waist_width = 2 * one_side_after_darts

    assert total_waist_width == pytest_approx(m.waist + intent.ease_bust_mm + intent.ease_waist_mm)


def test_necklines_have_no_backtracking():
    pieces = draft_bodice(_measurements(), _intent())
    # back's neckline runs away from CB (x increasing); front's is mirrored
    # and runs away from CF in the opposite direction (x decreasing)
    for piece_id, increasing in (("bodice_back", True), ("bodice_front", False)):
        piece = _piece(pieces, piece_id)
        pm = piece.point_map()
        seg = _edge(piece, "neckline").segments[0]
        poly = _sample_segment(pm, seg)

        xs = [p[0] for p in poly]
        if not increasing:
            xs = xs[::-1]
        assert all(b >= a - 1e-6 for a, b in zip(xs, xs[1:])), f"{piece_id} neckline x backtracks"


def test_neckline_meets_the_fold_line_with_a_horizontal_tangent():
    """Each piece is cut on the fold at CB (back) / CF (front) — x=0 in its
    own local coordinates — and gets mirrored across that line for the
    actual fabric piece. A curve touching x=0 only mirrors into a smooth
    continuation if its tangent there is horizontal (zero y-component):
    reflecting x negates the x-component of the tangent but not the
    y-component, so a nonzero y-component means the original curve and its
    mirror image arrive at the shared point heading in different
    directions — a visible cusp, not a smooth curve. Caught by actually
    mirroring a piece into its full cut-on-the-fold shape and rendering it
    (the half-piece view this project normally works from hides it
    completely, since there's nothing on the other side to clash with).

    Checked via the control point adjacent to the fold-line endpoint: for
    a cubic bezier, the tangent at an endpoint is parallel to the vector
    from that endpoint to its adjacent control point, so a horizontal
    tangent means that control point shares the endpoint's y-coordinate.
    """
    pieces = draft_bodice(_measurements(), _intent())
    for piece_id, fold_point_id in (("bodice_back", "cb_neck"), ("bodice_front", "cf_neck")):
        piece = _piece(pieces, piece_id)
        pm = piece.point_map()
        seg = _edge(piece, "neckline").segments[0]
        fold_y = pm[fold_point_id].y

        if seg.start == fold_point_id:
            control_y = seg.c1[1]
        else:
            assert seg.end == fold_point_id
            control_y = seg.c2[1]

        assert control_y == pytest_approx(fold_y, abs=1e-6), (
            f"{piece_id} neckline tangent at {fold_point_id} isn't horizontal — will mirror into a cusp"
        )


# --------------------------------------------------------------------------
# The armhole curve direction (this module's main fix)
# --------------------------------------------------------------------------
def test_armhole_curves_are_concave_not_a_straight_bulge():
    """A real armhole pulls in tight near the underarm rather than
    bulging out uniformly — the whole reason this module exists is a
    rebuild of a version that got this backwards (a 0.18-0.32x-of-chord
    outward bulge, peaking at 81.7mm on one real test case). This checks
    the curve deviates from its own chord by a modest, non-degenerate
    amount: not a straight line (no deviation at all), and nowhere near
    the old bug's scale."""
    pieces = draft_bodice(_measurements(), _intent())
    for piece_id in ("bodice_front", "bodice_back"):
        piece = _piece(pieces, piece_id)
        pm = piece.point_map()
        edge = _edge(piece, "armhole")
        full_poly: list[tuple[float, float]] = []
        for seg in edge.segments:
            full_poly.extend(_sample_segment(pm, seg))

        deviation = _max_perpendicular_deviation(full_poly)
        assert 5.0 < deviation < 60.0, f"{piece_id} armhole bulge {deviation:.1f}mm looks wrong"


def test_armhole_is_not_a_pinched_zigzag():
    """A pinched/self-crossing curve (this module's second real bug,
    caught by rendering and looking — see the module docstring's note on
    A1) makes the curve double back on itself far more than a smooth
    concave pull needs to, which shows up as an arc length much longer
    than the straight chord between its endpoints. A real armhole's
    concave pull near the underarm is gentle: the un-pinched construction
    here runs about 1.25-1.3x its chord length; the pinched version this
    guards against was well over 2x, since it travels in and back out
    past both endpoints' own x-range rather than pulling in modestly."""
    pieces = draft_bodice(_measurements(), _intent())
    for piece_id in ("bodice_front", "bodice_back"):
        piece = _piece(pieces, piece_id)
        pm = piece.point_map()
        edge = _edge(piece, "armhole")
        start = pm[edge.segments[0].start]
        end = pm[edge.segments[-1].end]
        chord = math.hypot(end.x - start.x, end.y - start.y)

        assert _edge_length(piece, "armhole") < chord * 1.8, f"{piece_id} armhole looks pinched"


# --------------------------------------------------------------------------
# Pivot/compass shoulder construction
# --------------------------------------------------------------------------
def test_back_shoulder_pivot_radius():
    """O (tutorial steps 16-18): pivot from J with radius =
    shoulder_length + 1.5cm (the shoulder-dart ease), until it meets the
    horizontal guideline through N — the same pivot construction the
    front shoulder tip uses (see test_front_shoulder_pivot_radius below),
    just with an added ease and a different guideline/pivot point. An
    earlier direct coordinate formula for O (not referencing
    shoulder_length at all) has been replaced with this."""
    m = _measurements()
    pieces = draft_bodice(m, _intent())
    back = _piece(pieces, "bodice_back")
    pm = back.point_map()

    back_radius = _dist(pm, "back_neck_side", "back_shoulder_tip")
    assert back_radius == pytest_approx(m.shoulder_length + 15.0)  # +1.5cm ease, in mm


def test_front_shoulder_pivot_radius():
    """The front shoulder tip is still found by pivoting shoulder_length
    (no ease) from the dart point T — unlike the back, which now uses a
    direct coordinate formula for O instead of a pivot (see
    test_back_shoulder_tip_matches_the_direct_formula)."""
    m = _measurements()
    pieces = draft_bodice(m, _intent())
    front = _piece(pieces, "bodice_front")
    front_pm = front.point_map()

    front_radius = _dist(front_pm, "front_shoulder_dart_leg_t", "front_shoulder_tip")
    assert front_radius == pytest_approx(m.shoulder_length)


def test_back_shoulder_dart_apex_matches_the_tutorial_steps():
    """Q (the back shoulder dart apex) follows the tutorial's own steps
    20-22 point-for-point, not a new construction: 15cm down from A on
    centre back (independent of P's own position), at P's x, then shifted
    3cm toward centre back. Checked in stored mm/y-down terms, so "15cm
    down from A" (y-up) becomes a plain +150mm from cb_neck."""
    pieces = draft_bodice(_measurements(), _intent())
    back = _piece(pieces, "bodice_back")
    pm = back.point_map()

    p = pm["back_shoulder_dart_leg_p"]
    apex = pm["back_shoulder_dart_apex"]
    assert apex.x == pytest_approx(p.x - 30.0)
    assert apex.y == pytest_approx(150.0)


def test_back_shoulder_dart_leg_r_is_a_short_step_from_p_toward_o():
    """R (step 23): 1.5cm from P along the shoulder seam itself (the J-O
    line), toward O — not related to Q's construction at all."""
    pieces = draft_bodice(_measurements(), _intent())
    back = _piece(pieces, "bodice_back")
    pm = back.point_map()

    p, r, o = pm["back_shoulder_dart_leg_p"], pm["back_shoulder_dart_leg_r"], pm["back_shoulder_tip"]
    assert _dist(pm, "back_shoulder_dart_leg_p", "back_shoulder_dart_leg_r") == pytest_approx(15.0)
    # R is between P and O (closer to O), i.e. a short step FROM P TOWARD O
    assert _dist(pm, "back_shoulder_dart_leg_r", "back_shoulder_tip") < _dist(pm, "back_shoulder_dart_leg_p", "back_shoulder_tip")


def test_shoulder_tip_is_farther_from_centre_than_the_dart_points():
    """A sign error in the pivot direction (this module's third bug) puts
    the shoulder tip CLOSER to centre front/back than the dart points
    that are supposed to lead up to it — geometrically backwards for a
    shoulder seam, which should move steadily away from centre as it
    runs out to the arm."""
    pieces = draft_bodice(_measurements(), _intent())
    front = _piece(pieces, "bodice_front")
    back = _piece(pieces, "bodice_back")
    front_pm, back_pm = front.point_map(), back.point_map()

    assert back_pm["back_shoulder_tip"].x > back_pm["back_shoulder_dart_leg_r"].x > back_pm["back_neck_side"].x
    assert front_pm["front_shoulder_tip"].x > front_pm["front_shoulder_dart_leg_t"].x > front_pm["front_neck_side"].x


# --------------------------------------------------------------------------
# Dart structure
# --------------------------------------------------------------------------
def test_each_piece_has_a_shoulder_dart_and_a_waist_dart_but_no_side_dart():
    """The side seam's suppression is cut off directly (see
    _draft_back/_draft_front), not folded as a dart -- so it's not a Dart
    object at all, unlike the shoulder and waist darts."""
    pieces = draft_bodice(_measurements(), _intent())
    front = _piece(pieces, "bodice_front")
    back = _piece(pieces, "bodice_back")

    assert {d.id for d in front.darts} == {"front_shoulder_dart", "bust_dart"}
    assert {d.id for d in back.darts} == {"back_shoulder_dart", "back_waist_dart"}


def test_front_dart_apexes_straddle_the_bust_point():
    pieces = draft_bodice(_measurements(), _intent())
    front = _piece(pieces, "bodice_front")
    pm = front.point_map()

    bust = pm["bust_point"]
    shoulder_apex = pm["front_shoulder_dart_apex"]
    waist_apex = pm["front_waist_dart_apex"]

    assert shoulder_apex.y < bust.y < waist_apex.y


def test_vertical_bust_line_points_are_colinear_with_the_bust_point():
    """R1 (tutorial's "R", renamed to avoid clashing with the back's own
    R) and S (step 26) mark where the vertical bust line crosses line CG
    and the waistline, respectively — U (step 30, the front shoulder dart
    apex) is then placed directly on that same line, 1cm above the bust
    point. All four share one x-coordinate by construction."""
    pieces = draft_bodice(_measurements(), _intent())
    front = _piece(pieces, "bodice_front")
    pm = front.point_map()

    bust = pm["bust_point"]
    r1 = pm["vertical_bust_line_r1"]
    s = pm["vertical_bust_line_s"]
    u = pm["front_shoulder_dart_apex"]

    assert r1.x == pytest_approx(bust.x)
    assert s.x == pytest_approx(bust.x)
    assert u.x == pytest_approx(bust.x)
    # r1 (near the top, on line CG) sits above s (on the waistline, near
    # the bottom) in stored mm/y-down terms
    assert r1.y < s.y


def test_bust_dart_width_increases_with_bust():
    small = draft_bodice(_measurements(bust=32 * _IN_TO_MM), _intent())
    large = draft_bodice(_measurements(bust=40 * _IN_TO_MM), _intent())

    def _dart_width(pieces):
        front = _piece(pieces, "bodice_front")
        pm = front.point_map()
        return _dist(pm, "front_neck_side", "front_shoulder_dart_leg_t")

    assert _dart_width(large) > _dart_width(small)


# --------------------------------------------------------------------------
# Measurement plumbing
# --------------------------------------------------------------------------
def test_back_width_is_halved_and_given_construction_ease():
    """back_width is a FULL across-back measurement (step 33: 'half of
    your back measurement'); the first version of this module treated a
    supplied value as already-halved."""
    m = _measurements(back_width=300.0)
    pieces = draft_bodice(m, _intent())
    back = _piece(pieces, "bodice_back")
    pm = back.point_map()

    # underarm_back = midpoint(W, A1); isolate W's contribution by using
    # the back waist dart's x (step 43: midpoint of H and W, i.e. W/2)
    w_x = pm["back_waist_dart_apex"].x * 2
    assert w_x == pytest_approx(300.0 / 2 + _BACK_WIDTH_EASE_MM)


def test_supplied_neck_circumference_sets_back_neck_width_directly():
    m = _measurements(neck_circumference=400.0)
    pieces = draft_bodice(m, _intent())
    back = _piece(pieces, "bodice_back")
    assert back.point_map()["back_neck_side"].x == pytest_approx(400.0 / 5)


def test_a1_sits_left_of_where_tu_crosses_hi_by_distance_u_to_e():
    """A1, corrected: not the bust-point-offset-from-W reading of step 36
    anymore. Instead: find where line T-U (the front shoulder dart's
    T-to-apex leg) crosses line HI, then shift that point toward centre
    back (left) by distance(U, E). Checked directly against
    `_shared_frame`'s own construction-space points, since A1 itself
    isn't a stored Point (only A2/A3/A4, which are built from it, are)."""
    m = _measurements()
    s = _shared_frame(m, _intent())

    t, u, e, i = s.front_shoulder_dart_leg_t, s.front_shoulder_dart_apex, s.E, s.I
    t_param = (i[1] - t[1]) / (u[1] - t[1])
    hi_tu_x = t[0] + t_param * (u[0] - t[0])
    dist_u_e = math.hypot(u[0] - e[0], u[1] - e[1])
    expected_a1_x = hi_tu_x - dist_u_e

    # A3 = midpoint(W, A1); back out A1 from the stored A3 and W (via the
    # back waist dart apex, which is W/2 -- see test_back_width_is_halved...)
    pieces = draft_bodice(m, _intent())
    back = _piece(pieces, "bodice_back")
    w_x_mm = back.point_map()["back_waist_dart_apex"].x * 2
    a3_x_mm = back.point_map()["underarm_back"].x
    a1_x_mm = 2 * a3_x_mm - w_x_mm

    assert a1_x_mm == pytest_approx(expected_a1_x * 10)


def test_c2_sits_directly_below_c1_on_the_back_waistline():
    """C2 (back piece): same x as C1 (back_waist_dart_apex), same y as B
    (the back's own waistline) -- the base the back waist dart's two legs
    straddle."""
    pieces = draft_bodice(_measurements(), _intent())
    back = _piece(pieces, "bodice_back")
    pm = back.point_map()

    c1 = pm["back_waist_dart_apex"]
    c2 = pm["back_waist_dart_c2"]
    cb_waist = pm["cb_waist"]

    assert c2.x == pytest_approx(c1.x)
    assert c2.y == pytest_approx(cb_waist.y)


def test_underarm_waist_point_sits_below_underarm_on_the_flat_waistline():
    """A4 (step 39, second half): straight down from A3 (the underarm
    point) to the FLAT waistline — y=-nape_to_waist in the cm/y-up
    construction space, before step 47's balance adjustment. Same x as
    A3, same y as the back's own cb_waist (since both sit on that flat,
    unbalanced line)."""
    pieces = draft_bodice(_measurements(), _intent())
    back = _piece(pieces, "bodice_back")
    pm = back.point_map()

    underarm = pm["underarm_back"]
    underarm_waist = pm["underarm_waist_back"]
    cb_waist = pm["cb_waist"]

    assert underarm_waist.x == pytest_approx(underarm.x)
    assert underarm_waist.y == pytest_approx(cb_waist.y)


def test_armhole_depth_increases_with_bust_when_not_directly_measured():
    small = draft_bodice(_measurements(bust=32 * _IN_TO_MM, armscye_depth=None), _intent())
    large = draft_bodice(_measurements(bust=42 * _IN_TO_MM, armscye_depth=None), _intent())

    small_depth = _piece(small, "bodice_back").point_map()["underarm_back"].y
    large_depth = _piece(large, "bodice_back").point_map()["underarm_back"].y
    assert large_depth > small_depth


# --------------------------------------------------------------------------
# draft_bodice_from_sloper (Phase 6 sketch) — a reference sloper's points
# may differ from what the formulas alone would give (a muslin-fitting
# correction); that per-point difference should carry forward onto a
# fresh draft for different measurements, not get discarded or copied
# as an absolute coordinate.
# --------------------------------------------------------------------------
def test_draft_from_sloper_matches_plain_formulas_when_the_sloper_has_no_correction():
    """A sloper that's exactly what the formulas would produce anyway (no
    muslin correction baked in) should adapt to a no-op: identical to a
    plain draft_bodice() call for the same inputs."""
    m, intent = _measurements(), _intent()
    sloper_pieces = draft_bodice(m, intent)

    adapted = draft_bodice_from_sloper(sloper_pieces, m, intent, m, intent)
    plain = draft_bodice(m, intent)

    for adapted_piece, plain_piece in zip(
        sorted(adapted, key=lambda p: p.id), sorted(plain, key=lambda p: p.id),
    ):
        for pid, plain_pt in plain_piece.point_map().items():
            adapted_pt = adapted_piece.point_map()[pid]
            assert adapted_pt.x == pytest_approx(plain_pt.x)
            assert adapted_pt.y == pytest_approx(plain_pt.y)


def test_draft_from_sloper_carries_a_manual_correction_onto_a_different_body():
    """Simulates a muslin fitting: nudge one point (the back shoulder
    dart's apex) by a known offset in the sloper, away from whatever the
    formulas gave it. Drafting from that sloper for a DIFFERENT bust
    measurement should land that same point the formula's own fresh
    position for the new body, PLUS the identical offset — not the
    sloper's raw absolute coordinate (which wouldn't make sense on a
    different-sized body), and not the plain formula value either (which
    would silently drop the fitting correction)."""
    sloper_m, intent = _measurements(), _intent()
    sloper_pieces = list(draft_bodice(sloper_m, intent))
    back = next(p for p in sloper_pieces if p.id == "bodice_back")
    pm = back.point_map()
    apex = pm["back_shoulder_dart_apex"]
    dx, dy = 4.0, -2.5  # a hand-made correction, in mm
    corrected_points = [
        dataclasses.replace(p, x=p.x + dx, y=p.y + dy) if p.id == "back_shoulder_dart_apex" else p
        for p in back.points
    ]
    corrected_back = dataclasses.replace(back, points=corrected_points)
    sloper_pieces = [corrected_back if p.id == "bodice_back" else p for p in sloper_pieces]

    new_m = _measurements(bust=40 * _IN_TO_MM)
    adapted = draft_bodice_from_sloper(sloper_pieces, sloper_m, intent, new_m, intent)
    fresh_for_new_body = draft_bodice(new_m, intent)

    adapted_apex = _piece(adapted, "bodice_back").point_map()["back_shoulder_dart_apex"]
    fresh_apex = _piece(fresh_for_new_body, "bodice_back").point_map()["back_shoulder_dart_apex"]

    assert adapted_apex.x == pytest_approx(fresh_apex.x + dx)
    assert adapted_apex.y == pytest_approx(fresh_apex.y + dy)


def test_draft_from_sloper_leaves_unrelated_points_at_their_fresh_formula_value():
    """Only the corrected point should move -- the correction is applied
    per-point, not as a whole-piece shift."""
    sloper_m, intent = _measurements(), _intent()
    sloper_pieces = list(draft_bodice(sloper_m, intent))
    back = next(p for p in sloper_pieces if p.id == "bodice_back")
    corrected_points = [
        dataclasses.replace(p, x=p.x + 4.0, y=p.y - 2.5) if p.id == "back_shoulder_dart_apex" else p
        for p in back.points
    ]
    sloper_pieces = [
        dataclasses.replace(back, points=corrected_points) if p.id == "bodice_back" else p
        for p in sloper_pieces
    ]

    new_m = _measurements(bust=40 * _IN_TO_MM)
    adapted = draft_bodice_from_sloper(sloper_pieces, sloper_m, intent, new_m, intent)
    fresh_for_new_body = draft_bodice(new_m, intent)

    adapted_cb_waist = _piece(adapted, "bodice_back").point_map()["cb_waist"]
    fresh_cb_waist = _piece(fresh_for_new_body, "bodice_back").point_map()["cb_waist"]
    assert adapted_cb_waist.x == pytest_approx(fresh_cb_waist.x)
    assert adapted_cb_waist.y == pytest_approx(fresh_cb_waist.y)
