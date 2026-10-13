"""Fitted bodice block — front and back, half-body (mirrored) pieces.

Follows "How to Draft a Bodice Block" (inthefolds.com, itself adapted from
Winifred Aldrich's *Metric Pattern Cutting for Women's Wear*), per a
point-by-point construction supplied directly by the user rather than
re-derived from the fetched tutorial text. Differences from that earlier
derivation (see docs/CASE_STUDY.md for the full history):

- **Units and orientation.** The user's construction works in CENTIMETRES
  with y increasing UPWARD (nape at the origin, everything else at
  negative y). The rest of this project stores geometry in MILLIMETRES
  with y increasing DOWNWARD (see schema.py's own docstring). Rather than
  change that project-wide convention, this module does every step of the
  actual construction in the user's cm/y-up space — so the formulas below
  can be checked against their own written spec directly — and converts
  to the stored mm/y-down convention in exactly one place: `_to_mm()`,
  used at the point each `Point()` is created. Nothing outside this file
  needs to know the construction happens in a different space internally.
- **Points given explicitly, lettered to match:** A, B, C, D, E, F, G, H,
  I, J, K, L, N below are exactly the user's own coordinates. Two were
  genuinely ambiguous as first written and confirmed directly: H/I's
  "+5" is +0.5cm of ease (not +5cm), and N is a point ON centre back
  (x=0) at a guideline height, not an x-offset point at nape height.
- **Back neckline (A to J):** the user's own parabola, y = 1.5*(x/J.x)^2
  — not a hand-tuned bezier. A parabola has an EXACT cubic-bezier
  representation (degree-elevated from the equivalent quadratic); see
  `_parabola_to_cubic()`.
- **Front neckline (K to L):** the user's own "perfect 1/4 circle" —
  implemented with the standard kappa=0.5522847498 cubic-bezier circular
  arc approximation (error under 0.03% of the radius — as close to exact
  as a cubic bezier gets; a mathematically exact circular arc needs a
  rational bezier/NURBS, which this schema's `Segment` doesn't support).
  See `_quarter_circle_to_cubic()`.

Everything from the back shoulder pivot (O) onward — shoulder darts, bust
point, front shoulder dart, armhole, waist darts, side seam, waistline
balance — is carried over from the previous version's construction
(itself read from the fetched tutorial text), translated into this same
cm/y-up space, PENDING the user supplying their own point-by-point spec
for those steps too. Each such formula is commented with where it came
from and is expected to be replaced, not treated as equally authoritative
with the explicitly-specified points above.

Still unverified against a muslin — see CLAUDE.md.
"""
from __future__ import annotations

import dataclasses
import math
from typing import NamedTuple

from api.schema import (
    DesignIntent, Dart, Edge, EdgeRole, Grainline, Measurements, Notch,
    PatternPiece, Point, Segment, SegmentKind,
)

_CM_TO_MM = 10.0
_KAPPA = 0.5522847498307936  # standard cubic-bezier circular-arc constant

# ---- constants for the steps NOT YET given explicitly by the user, ----
# ---- carried over from the previous (tutorial-text-derived) version  ----
_BACK_SHOULDER_DART_EASE_CM = 1.5    # tutorial step 18
_BACK_DART_DOWN_FROM_NAPE_CM = 15.0  # tutorial step 20
_BACK_DART_TOWARD_CB_CM = 3.0        # tutorial step 22
_BACK_DART_LEG_R_CM = 1.5            # tutorial step 23
_BUST_POINT_EASE_CM = 0.5
_FRONT_GUIDE_UP_FROM_L_CM = 0.5
_BUST_DART_BASE_WIDTH_CM = 7.0
_BUST_DART_BASELINE_BUST_CM = 88.0
_BUST_DART_INCREMENT_BUST_CM = 4.0
_BUST_DART_INCREMENT_WIDTH_CM = 0.6
_DART_APEX_STANDOFF_CM = 1.0
_BACK_WIDTH_EASE_CM = 0.5
_B1_DIAGONAL_CM = 2.5
_B2_DIAGONAL_CM = 1.5
_SIDE_SEAM_BACK_SHARE = 1.0 / 3.0
_SIDE_SEAM_FRONT_SHARE = 2.0 / 3.0
_WAIST_BALANCE_DROP_CM = 1.0

# Fallbacks (cm), used only when a measurement wasn't supplied.
_ARMSCYE_DEPTH_FALLBACK_ADD_CM = 5.0
_BACK_WIDTH_FALLBACK_ADD_CM = 3.0
_SHOULDER_LENGTH_FALLBACK_CM = 12.5
_NAPE_TO_BUST_FALLBACK_FRACTION = 0.68


def _to_mm(pt_cm: tuple[float, float]) -> tuple[float, float]:
    """The one place construction-space (cm, y-up) becomes stored-schema
    space (mm, y-down, per schema.py's own docstring)."""
    return (pt_cm[0] * _CM_TO_MM, -pt_cm[1] * _CM_TO_MM)


def _pivot(px: float, py: float, radius: float, guide_y: float, direction: float) -> tuple[float, float]:
    """Compass/pivot move: from (px, py), swing an arc of length `radius`
    until it meets the horizontal guideline y=guide_y. `direction` +1
    solves toward larger x (back, away from centre back at x=0); -1
    toward smaller x (front, away from centre front at x=bustline).
    Clamps to the guideline (rather than raising) if the radius is too
    short to reach it, so a malformed measurement can't crash drafting.
    """
    dy = guide_y - py
    remaining_sq = radius * radius - dy * dy
    dx = math.sqrt(remaining_sq) if remaining_sq > 0 else 0.0
    return (px + direction * dx, guide_y)


def _dart_point_out(
    leg_a: tuple[float, float], leg_b: tuple[float, float], apex: tuple[float, float],
) -> tuple[float, float]:
    """A dart's "point" (D1): the midpoint of its two legs, pushed outward
    — away from the dart's own apex, i.e. away from the inside of the
    pattern piece — by half the distance from that midpoint to either leg.
    The straight line from leg to leg is replaced with two lines,
    leg_a-D1 and D1-leg_b, giving the dart a little extra fabric at the
    seam edge so that once it's folded and sewn shut, the edge lies flush
    with the rest of the seamline instead of coming up short there.

    Which of the midpoint's two perpendicular directions is "outward" is
    resolved locally, against the apex specifically (always a point well
    inside the piece) rather than any whole-piece convention, since this
    is a local, per-dart construction, not a general offset.
    """
    mid = ((leg_a[0] + leg_b[0]) / 2, (leg_a[1] + leg_b[1]) / 2)
    dist = math.hypot(leg_a[0] - mid[0], leg_a[1] - mid[1]) / 2
    dx, dy = leg_b[0] - leg_a[0], leg_b[1] - leg_a[1]
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    perp_a, perp_b = (-uy, ux), (uy, -ux)
    away = (mid[0] - apex[0], mid[1] - apex[1])
    perp = perp_a if (perp_a[0] * away[0] + perp_a[1] * away[1]) > (perp_b[0] * away[0] + perp_b[1] * away[1]) \
        else perp_b
    return (mid[0] + perp[0] * dist, mid[1] + perp[1] * dist)


def _bf_y(x: float, B: tuple[float, float], F: tuple[float, float]) -> float:
    """The y-coordinate at horizontal position `x` (shared-frame, 0=CB to
    bustline=CF) along the straight, balanced waistline from B to F (step
    47) -- not the flat line B itself sits on. Every waist dart's legs
    land on this sloped line, not the flat one."""
    t = x / F[0]
    return B[1] + t * (F[1] - B[1])


def _parabola_to_cubic(
    p0: tuple[float, float], p3: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Exact cubic-bezier control points for the parabola through p0=(0,0)
    and p3=(w, k*w^2) (y = k*x^2, p0 at the vertex) — degree-elevated from
    the equivalent quadratic bezier (control point at (w/2, 0), which
    traces the same parabola exactly: x(t)=w*t, y(t)=k*w^2*t^2)."""
    w, kw2 = p3[0] - p0[0], p3[1] - p0[1]
    q = (p0[0] + w / 2, p0[1])
    c1 = (p0[0] + 2 / 3 * (q[0] - p0[0]), p0[1] + 2 / 3 * (q[1] - p0[1]))
    c2 = (p3[0] + 2 / 3 * (q[0] - p3[0]), p3[1] + 2 / 3 * (q[1] - p3[1]))
    return c1, c2


def _quarter_circle_to_cubic(
    center: tuple[float, float], p0: tuple[float, float], p3: tuple[float, float], radius: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Standard kappa cubic-bezier approximation of a quarter circle from
    p0 to p3 around `center` (p0 and p3 each `radius` from it, 90 degrees
    apart) — as close to a "perfect 1/4 circle" as a cubic bezier gets."""
    u0 = ((p0[0] - center[0]) / radius, (p0[1] - center[1]) / radius)
    u3 = ((p3[0] - center[0]) / radius, (p3[1] - center[1]) / radius)
    # Sweep direction (CCW if the cross product u0 x u3 is positive, else
    # CW) — the tangent for CCW motion at angle theta is (-sin,cos), i.e.
    # u rotated +90 degrees: (ux,uy) -> (-uy,ux); CW is the -90 degree
    # rotation (uy,-ux). `sign` picks between them with one formula.
    cross = u0[0] * u3[1] - u0[1] * u3[0]
    sign = 1.0 if cross > 0 else -1.0
    t0 = (-u0[1] * sign, u0[0] * sign)
    t3 = (-u3[1] * sign, u3[0] * sign)
    # C1 leads away from p0 along the direction of travel; C2 is p3 pulled
    # BACK along that same direction (hence the subtraction, not another +).
    c1 = (p0[0] + _KAPPA * radius * t0[0], p0[1] + _KAPPA * radius * t0[1])
    c2 = (p3[0] - _KAPPA * radius * t3[0], p3[1] - _KAPPA * radius * t3[1])
    return c1, c2


class _Shared(NamedTuple):
    """Every point, in the user's own construction space: centimetres,
    y increasing UPWARD, origin at the nape (A). x=0 is centre back,
    x=bustline is centre front.
    """
    bustline: float
    nape_to_waist: float
    A: tuple[float, float]
    B: tuple[float, float]
    D: tuple[float, float]
    E: tuple[float, float]
    F: tuple[float, float]
    G: tuple[float, float]
    H: tuple[float, float]
    I: tuple[float, float]  # noqa: E741 — matches the tutorial's own letter
    J: tuple[float, float]
    K: tuple[float, float]
    L: tuple[float, float]
    N: tuple[float, float]
    back_neckline_c1: tuple[float, float]
    back_neckline_c2: tuple[float, float]
    front_neckline_c1: tuple[float, float]
    front_neckline_c2: tuple[float, float]
    back_shoulder_tip: tuple[float, float]       # O
    back_dart_apex: tuple[float, float]          # tutorial's "Q"
    back_dart_leg_p: tuple[float, float]         # P
    back_dart_leg_r: tuple[float, float]         # R
    bust_point: tuple[float, float]
    vertical_bust_line_r1: tuple[float, float]   # tutorial's "R" (renamed R1 — back already has an R)
    vertical_bust_line_s: tuple[float, float]    # S
    front_shoulder_dart_apex: tuple[float, float]  # U
    front_shoulder_dart_leg_t: tuple[float, float]  # T
    front_shoulder_tip: tuple[float, float]      # V
    back_armhole_c1: tuple[float, float]         # Z
    back_armhole_c2: tuple[float, float]         # B1
    front_armhole_c1: tuple[float, float]        # B2
    front_armhole_c2: tuple[float, float]        # A2
    underarm: tuple[float, float]                # A3
    underarm_waist: tuple[float, float]          # A4
    back_waist_dart_apex: tuple[float, float]    # tutorial's "C1" (back piece)
    back_waist_dart_base: tuple[float, float]    # tutorial's "C2" (back piece): same x as C1, on the waistline
    front_waist_dart_apex: tuple[float, float]
    cf_waist: tuple[float, float]                # balanced, step 47
    back_dart_intake: float
    front_dart_intake: float
    side_seam_back_share: float
    side_seam_front_share: float


def _shared_frame(m: Measurements, intent: DesignIntent) -> _Shared:
    bust = m.bust / _CM_TO_MM
    waist = m.waist / _CM_TO_MM
    ease = intent.ease_bust_mm / _CM_TO_MM
    ease_waist = intent.ease_waist_mm / _CM_TO_MM
    nape_to_waist = m.back_length / _CM_TO_MM
    nape_to_bust = (
        m.nape_to_bust / _CM_TO_MM if m.nape_to_bust is not None
        else nape_to_waist * _NAPE_TO_BUST_FALLBACK_FRACTION
    )
    armscye_depth = (
        m.armscye_depth / _CM_TO_MM if m.armscye_depth is not None
        else (bust + ease) / 4 + _ARMSCYE_DEPTH_FALLBACK_ADD_CM
    )
    neck_circ = (
        m.neck_circumference / _CM_TO_MM if m.neck_circumference is not None
        else (bust + ease) / 6 + 6.0
    )
    shoulder_length = (
        m.shoulder_length / _CM_TO_MM if m.shoulder_length is not None else _SHOULDER_LENGTH_FALLBACK_CM
    )
    back_width_full = (
        m.back_width / _CM_TO_MM if m.back_width is not None
        else (bust + ease) / 4 + _BACK_WIDTH_FALLBACK_ADD_CM
    )
    bust_span = m.bust_span / _CM_TO_MM

    bustline = (bust + ease) / 2

    A = (0.0, 0.0)
    B = (0.0, -nape_to_waist)
    C = (0.0, 1.5)
    D = (0.0, -nape_to_bust)
    E = (bustline, -nape_to_bust)
    # F, balanced (step 47): dropped from the flat waistline by the same
    # amount cf_waist used to be offset by -- F itself is now that lower
    # point, not a separate flat point with cf_waist offset from it. The
    # waist dart legs (below) are lengthened to land on the resulting
    # sloped B-F line rather than the flat line B sits on.
    F = (bustline, -nape_to_waist - _WAIST_BALANCE_DROP_CM)
    G = (bustline, 1.5)
    H = (0.0, -(armscye_depth + 0.5))
    I = (bustline, -(armscye_depth + 0.5))  # noqa: E741
    J = (neck_circ / 5, 1.5)
    K = (bustline - neck_circ / 5, 1.5)
    L = (bustline, 1.5 - neck_circ / 5)
    N = (0.0, -armscye_depth / 5 - 0.5)
    # O (tutorial steps 16-18): pivot from J with radius = shoulder_length
    # + 1.5cm (the shoulder-dart allowance), until it meets the horizontal
    # guideline through N. The direct coordinate formula tried earlier
    # (neck/5 + (armscye_depth/5+1)*sqrt(3)) didn't reference
    # shoulder_length at all and has been replaced with this.
    O = _pivot(J[0], J[1], shoulder_length + _BACK_SHOULDER_DART_EASE_CM, N[1], +1)  # noqa: E741
    del C  # not used by any formula below — see module docstring

    back_neckline_c1, back_neckline_c2 = _parabola_to_cubic(A, J)
    front_radius = neck_circ / 5
    front_neckline_c1, front_neckline_c2 = _quarter_circle_to_cubic(G, K, L, front_radius)

    back_shoulder_tip = O
    P = ((J[0] + O[0]) / 2, (J[1] + O[1]) / 2)  # midpoint of the J-O line

    # Q (dart apex) and R (dart's second leg) — the tutorial's own steps
    # 20-23, translated point-for-point into this cm/y-up space rather
    # than a new construction: a point 15cm down from A on centre back
    # (y=-15, independent of P entirely), squared across to meet a
    # vertical line dropped from P (giving x=P.x at that same y=-15), then
    # shifted 3cm toward centre back. R is a separate, much smaller step:
    # 1.5cm from P along the J-O line itself, toward O.
    back_dart_apex = (P[0] - _BACK_DART_TOWARD_CB_CM, -_BACK_DART_DOWN_FROM_NAPE_CM)  # Q
    s_dx, s_dy = back_shoulder_tip[0] - J[0], back_shoulder_tip[1] - J[1]
    s_len = math.hypot(s_dx, s_dy)
    R = (P[0] + s_dx / s_len * _BACK_DART_LEG_R_CM, P[1] + s_dy / s_len * _BACK_DART_LEG_R_CM)

    # ---- everything below: still carried over from the previous ----
    # ---- version, pending the user's own spec for these steps ----

    bust_point = (bustline - (bust_span / 2 + _BUST_POINT_EASE_CM), -nape_to_bust)
    # R1/S (tutorial step 26, "R" renamed to avoid clashing with the back's
    # own R): the VERTICAL BUST LINE — the vertical line through the bust
    # point — meets line CG (y=1.5, since C and G are both at that height)
    # at R1, and the waistline (line BF, y=-nape_to_waist) at S. Both are
    # reference points for this line, not something anything else in the
    # construction is measured FROM, besides U below (which the tutorial
    # itself places "on line RS").
    vertical_bust_line_r1 = (bust_point[0], 1.5)
    vertical_bust_line_s = (bust_point[0], -nape_to_waist)
    front_shoulder_dart_apex = (bust_point[0], bust_point[1] + _DART_APEX_STANDOFF_CM)  # U, on line R1-S

    dart_width = _BUST_DART_BASE_WIDTH_CM + _BUST_DART_INCREMENT_WIDTH_CM * (
        (bust - _BUST_DART_BASELINE_BUST_CM) / _BUST_DART_INCREMENT_BUST_CM
    )
    front_shoulder_dart_leg_t = (K[0] - dart_width, K[1])  # T, away from CF

    front_guide_y = L[1] + _FRONT_GUIDE_UP_FROM_L_CM
    front_shoulder_tip = _pivot(
        front_shoulder_dart_leg_t[0], front_shoulder_dart_leg_t[1], shoulder_length, front_guide_y, -1,
    )  # V

    w = (back_width_full / 2 + _BACK_WIDTH_EASE_CM, I[1])  # W
    x_pt = (w[0], N[1])                                    # X
    z = (w[0], (w[1] + x_pt[1]) / 2)                        # Z

    # A2 (step 38): armscye_depth/5 up the vertical from A1 (not all the
    # way to the shoulder seam it's squared toward). A3 (step 39):
    # midpoint of W and A1. A4 (step 39, second half): straight down from
    # A3 to the flat waistline (line BF, y=-nape_to_waist — the balance
    # adjustment in step 47 hasn't happened yet at this point).
    #
    # A1, corrected: where line T-U crosses line HI, then shifted left
    # (toward centre back) by distance(U, E) -- not the earlier
    # bust-point-offset-from-W reading of step 36.
    _tu_dx, _tu_dy = front_shoulder_dart_apex[0] - front_shoulder_dart_leg_t[0], \
        front_shoulder_dart_apex[1] - front_shoulder_dart_leg_t[1]
    _tu_t = (I[1] - front_shoulder_dart_leg_t[1]) / _tu_dy
    _hi_tu_x = front_shoulder_dart_leg_t[0] + _tu_t * _tu_dx
    _dist_u_e = math.hypot(front_shoulder_dart_apex[0] - E[0], front_shoulder_dart_apex[1] - E[1])
    a1 = (_hi_tu_x - _dist_u_e, I[1])                              # A1
    a2 = (a1[0], a1[1] + armscye_depth / 5)                       # A2
    underarm = ((w[0] + a1[0]) / 2, I[1])                         # A3
    underarm_waist = (underarm[0], -nape_to_waist)                # A4

    diag = 1.0 / math.sqrt(2.0)
    b1 = (w[0] + _B1_DIAGONAL_CM * diag, w[1] + _B1_DIAGONAL_CM * diag)
    b2 = (a1[0] - _B2_DIAGONAL_CM * diag, a1[1] + _B2_DIAGONAL_CM * diag)

    back_waist_dart_apex = (w[0] / 2, I[1])                       # C1 (back)
    back_waist_dart_base = (back_waist_dart_apex[0], B[1])        # C2 (back): same x as C1, same y as B
    front_waist_dart_apex = (bust_point[0], bust_point[1] - _DART_APEX_STANDOFF_CM)
    cf_waist = F

    total_suppression = bust - (waist + ease_waist)
    third = total_suppression / 2 / 3

    return _Shared(
        bustline=bustline, nape_to_waist=nape_to_waist,
        A=A, B=B, D=D, E=E, F=F, G=G, H=H, I=I, J=J, K=K, L=L, N=N,
        back_neckline_c1=back_neckline_c1, back_neckline_c2=back_neckline_c2,
        front_neckline_c1=front_neckline_c1, front_neckline_c2=front_neckline_c2,
        back_shoulder_tip=back_shoulder_tip, back_dart_apex=back_dart_apex,
        back_dart_leg_p=P, back_dart_leg_r=R, bust_point=bust_point,
        vertical_bust_line_r1=vertical_bust_line_r1, vertical_bust_line_s=vertical_bust_line_s,
        front_shoulder_dart_apex=front_shoulder_dart_apex,
        front_shoulder_dart_leg_t=front_shoulder_dart_leg_t, front_shoulder_tip=front_shoulder_tip,
        back_armhole_c1=z, back_armhole_c2=b1, front_armhole_c1=b2, front_armhole_c2=a2,
        underarm=underarm, underarm_waist=underarm_waist, back_waist_dart_apex=back_waist_dart_apex,
        back_waist_dart_base=back_waist_dart_base,
        front_waist_dart_apex=front_waist_dart_apex, cf_waist=cf_waist,
        back_dart_intake=third, front_dart_intake=third,
        side_seam_back_share=third * _SIDE_SEAM_BACK_SHARE, side_seam_front_share=third * _SIDE_SEAM_FRONT_SHARE,
    )


def _spline_through(points: list[tuple[float, float]]) -> list[tuple[tuple, tuple, tuple]]:
    """CENTRIPETAL Catmull-Rom spline (as cubic-bezier segments) through a
    chain of guide points — used for the armhole, which (pending the
    user's own spec for it) is still built from the previous version's
    seven-point construction rather than an explicit curve formula.

    This is centripetal (parametrized by chord-length^0.5 between
    consecutive points), not the simpler uniform version (every point
    treated as equally spaced) an earlier revision of this function used.
    Uniform Catmull-Rom's tangent at a point only looks at its neighbours'
    positions, not how FAR they are — so a point with one close neighbour
    and one far one gets a tangent skewed toward the close side, which
    shows up as a slightly uneven, bumpy curve when guide points (like
    these seven) aren't evenly spaced, which they aren't. Centripetal
    parametrization is the standard fix: it's the same construction, just
    weighted by actual distance, and is also guaranteed not to
    self-intersect or form cusps between points for any input, which the
    uniform version isn't. The curve still passes through every point
    exactly — this changes the shape BETWEEN them, not the points
    themselves.
    """
    n = len(points)
    alpha = 0.5  # centripetal

    def extended(i: int) -> tuple[float, float]:
        # a phantom point past either end, by linear reflection, so the
        # first/last real segment's tangent still has a "before"/"after"
        # neighbour to weigh against
        if i < 0:
            p0, p1 = points[0], points[1]
        else:
            p0, p1 = points[n - 1], points[n - 2]
        return (2 * p0[0] - p1[0], 2 * p0[1] - p1[1])

    def pt(i: int) -> tuple[float, float]:
        return points[i] if 0 <= i < n else extended(i)

    segments = []
    for i in range(n - 1):
        p0, p1, p2, p3 = pt(i - 1), points[i], points[i + 1], pt(i + 2)
        t0 = 0.0
        t1 = t0 + math.hypot(p1[0] - p0[0], p1[1] - p0[1]) ** alpha
        t2 = t1 + math.hypot(p2[0] - p1[0], p2[1] - p1[1]) ** alpha
        t3 = t2 + math.hypot(p3[0] - p2[0], p3[1] - p2[1]) ** alpha
        t1, t2, t3 = max(t1, t0 + 1e-9), max(t2, t1 + 1e-9), max(t3, t2 + 1e-9)

        m1x = (t2 - t1) * ((p1[0] - p0[0]) / (t1 - t0) - (p2[0] - p0[0]) / (t2 - t0) + (p2[0] - p1[0]) / (t2 - t1))
        m1y = (t2 - t1) * ((p1[1] - p0[1]) / (t1 - t0) - (p2[1] - p0[1]) / (t2 - t0) + (p2[1] - p1[1]) / (t2 - t1))
        m2x = (t2 - t1) * ((p2[0] - p1[0]) / (t2 - t1) - (p3[0] - p1[0]) / (t3 - t1) + (p3[0] - p2[0]) / (t3 - t2))
        m2y = (t2 - t1) * ((p2[1] - p1[1]) / (t2 - t1) - (p3[1] - p1[1]) / (t3 - t1) + (p3[1] - p2[1]) / (t3 - t2))

        c1 = (p1[0] + m1x / 3, p1[1] + m1y / 3)
        c2 = (p2[0] - m2x / 3, p2[1] - m2y / 3)
        segments.append((c1, c2, p2))
    return segments


def _full_armhole_spline(s: _Shared) -> list[tuple[tuple, tuple, tuple]]:
    """The armhole as ONE continuous curve through all seven of the
    tutorial's own points — O, Z, B1, A3, B2, A2, V — computed once, here,
    in the shared (unmirrored) frame, and split at A3 by the two draft
    functions below (first 3 segments to the back, last 3 to the front,
    mirrored). Doing this as one 7-point spline rather than two separate
    4-point splines (O-Z-B1-A3 and A3-B2-A2-V) that happen to share an
    endpoint matters at that shared point: a Catmull-Rom spline's tangent
    at an interior point is set from its actual neighbours on BOTH sides
    (B1 and B2 here), where two independent splines could only each guess
    a one-sided tangent at A3 — a small but real mismatch in the curve's
    direction exactly at the underarm, where both pieces' armholes meet
    when sewn."""
    return _spline_through([
        s.back_shoulder_tip, s.back_armhole_c1, s.back_armhole_c2, s.underarm,
        s.front_armhole_c1, s.front_armhole_c2, s.front_shoulder_tip,
    ])


def _draft_back(s: _Shared) -> PatternPiece:
    cb_neck = Point("cb_neck", *_to_mm(s.A))
    back_neck_side = Point("back_neck_side", *_to_mm(s.J))
    back_shoulder_tip = Point("back_shoulder_tip", *_to_mm(s.back_shoulder_tip))
    dart_apex = Point("back_shoulder_dart_apex", *_to_mm(s.back_dart_apex))
    dart_leg_p = Point("back_shoulder_dart_leg_p", *_to_mm(s.back_dart_leg_p))
    dart_leg_r = Point("back_shoulder_dart_leg_r", *_to_mm(s.back_dart_leg_r))
    dart_d1 = Point(
        "back_shoulder_dart_d1",
        *_dart_point_out((dart_leg_p.x, dart_leg_p.y), (dart_leg_r.x, dart_leg_r.y), (dart_apex.x, dart_apex.y)),
    )
    underarm = Point("underarm_back", *_to_mm(s.underarm))
    underarm_waist = Point("underarm_waist_back", *_to_mm(s.underarm_waist))
    cb_waist = Point("cb_waist", *_to_mm(s.B))

    waist_apex = Point("back_waist_dart_apex", *_to_mm(s.back_waist_dart_apex))
    waist_c2 = Point("back_waist_dart_c2", *_to_mm(s.back_waist_dart_base))
    # Legs land on the sloped, balanced B-F line (step 47), not the flat
    # line B itself sits on -- lengthened down to meet it.
    leg_a_x = s.back_waist_dart_base[0] - s.back_dart_intake / 2
    leg_b_x = s.back_waist_dart_base[0] + s.back_dart_intake / 2
    leg_a = Point("back_waist_dart_leg_a", *_to_mm((leg_a_x, _bf_y(leg_a_x, s.B, s.F))))
    leg_b = Point("back_waist_dart_leg_b", *_to_mm((leg_b_x, _bf_y(leg_b_x, s.B, s.F))))
    waist_d1 = Point(
        "back_waist_dart_d1",
        *_dart_point_out((leg_a.x, leg_a.y), (leg_b.x, leg_b.y), (waist_apex.x, waist_apex.y)),
    )

    # Side seam: the side seam's share of the total waist suppression is
    # cut off directly -- the seam itself angles in to a single inset
    # point on the waistline -- rather than folded and sewn as a dart
    # like the other two. Not a Dart at all: no apex, no second leg, no
    # dashed fold-line annotation.
    side_waist_x = s.underarm[0] - s.side_seam_back_share
    side_waist = Point("side_waist_back", *_to_mm((side_waist_x, _bf_y(side_waist_x, s.B, s.F))))

    neck_c1, neck_c2 = _to_mm(s.back_neckline_c1), _to_mm(s.back_neckline_c2)

    armhole_z = Point("back_armhole_z", *_to_mm(s.back_armhole_c1))
    armhole_b1 = Point("back_armhole_b1", *_to_mm(s.back_armhole_c2))
    full_armhole = _full_armhole_spline(s)[:3]  # O-Z, Z-B1, B1-A3 -- see _full_armhole_spline
    armhole_segments = [
        Segment(sid, eid, SegmentKind.CUBIC, _to_mm(c1), _to_mm(c2))
        for sid, eid, (c1, c2, _end) in zip(
            ["back_shoulder_tip", "back_armhole_z", "back_armhole_b1"],
            ["back_armhole_z", "back_armhole_b1", "underarm_back"],
            full_armhole,
        )
    ]

    edges = [
        Edge(id="neckline", role=EdgeRole.NECKLINE, segments=[
            Segment("cb_neck", "back_neck_side", SegmentKind.CUBIC, neck_c1, neck_c2),
        ]),
        Edge(id="shoulder", role=EdgeRole.SEAM, segments=[
            Segment("back_neck_side", "back_shoulder_dart_leg_p", SegmentKind.LINE),
            Segment("back_shoulder_dart_leg_p", "back_shoulder_dart_d1", SegmentKind.LINE),
            Segment("back_shoulder_dart_d1", "back_shoulder_dart_leg_r", SegmentKind.LINE),
            Segment("back_shoulder_dart_leg_r", "back_shoulder_tip", SegmentKind.LINE),
        ]),
        Edge(id="armhole", role=EdgeRole.ARMHOLE, segments=armhole_segments),
        Edge(id="side_seam", role=EdgeRole.SEAM, segments=[
            Segment("underarm_back", "side_waist_back", SegmentKind.LINE),
        ]),
        Edge(id="waist", role=EdgeRole.SEAM, segments=[
            Segment("cb_waist", "back_waist_dart_leg_a", SegmentKind.LINE),
            Segment("back_waist_dart_leg_a", "back_waist_dart_d1", SegmentKind.LINE),
            Segment("back_waist_dart_d1", "back_waist_dart_leg_b", SegmentKind.LINE),
            Segment("back_waist_dart_leg_b", "side_waist_back", SegmentKind.LINE),
        ]),
        Edge(id="center_back", role=EdgeRole.SEAM, segments=[
            Segment("cb_neck", "cb_waist", SegmentKind.LINE),
        ]),
    ]

    return PatternPiece(
        id="bodice_back", name="Bodice back", cut_count=1, cut_mirrored=True,
        points=[
            cb_neck, back_neck_side, back_shoulder_tip, dart_apex, dart_leg_p, dart_leg_r, dart_d1,
            armhole_z, armhole_b1, underarm, underarm_waist, cb_waist,
            waist_apex, waist_c2, leg_a, leg_b, waist_d1, side_waist,
        ],
        edges=edges,
        darts=[
            Dart(id="back_shoulder_dart", apex="back_shoulder_dart_apex",
                 leg_a="back_shoulder_dart_leg_p", leg_b="back_shoulder_dart_leg_r"),
            Dart(id="back_waist_dart", apex="back_waist_dart_apex",
                 leg_a="back_waist_dart_leg_a", leg_b="back_waist_dart_leg_b"),
        ],
        notches=[Notch(id="back_armhole_notch", point="underarm_back", count=1)],
        grainline=Grainline(start="cb_neck", end="cb_waist"),
    )


def _draft_front(s: _Shared) -> PatternPiece:
    # Front is mirrored across centre front (x=bustline -> local x=0, away
    # from it is +x), still entirely within the cm/y-up construction space
    # — _to_mm() is applied last, after this transform.
    def mirror(pt: tuple[float, float]) -> tuple[float, float]:
        return (s.bustline - pt[0], pt[1])

    cf_neck = Point("cf_neck", *_to_mm(mirror(s.L)))
    front_neck_side = Point("front_neck_side", *_to_mm(mirror(s.K)))
    dart_leg_t = Point("front_shoulder_dart_leg_t", *_to_mm(mirror(s.front_shoulder_dart_leg_t)))
    dart_apex = Point("front_shoulder_dart_apex", *_to_mm(mirror(s.front_shoulder_dart_apex)))
    front_shoulder_tip = Point("front_shoulder_tip", *_to_mm(mirror(s.front_shoulder_tip)))
    bust_point = Point("bust_point", *_to_mm(mirror(s.bust_point)))
    vertical_bust_line_r1 = Point("vertical_bust_line_r1", *_to_mm(mirror(s.vertical_bust_line_r1)))
    vertical_bust_line_s = Point("vertical_bust_line_s", *_to_mm(mirror(s.vertical_bust_line_s)))
    underarm = Point("underarm_front", *_to_mm(mirror(s.underarm)))
    underarm_waist = Point("underarm_waist_front", *_to_mm(mirror(s.underarm_waist)))
    cf_waist = Point("cf_waist", *_to_mm(mirror(s.cf_waist)))

    waist_apex = Point("front_waist_dart_apex", *_to_mm(mirror(s.front_waist_dart_apex)))
    # centred on S (same x as the bust point, on the waistline). Built in
    # the shared (unmirrored) frame first, since the sloped B-F line (step
    # 47) is itself defined in that frame, then mirrored as a whole point
    # — see the matching back-piece comment on _bf_y.
    #
    # leg_a gets the LARGER shared-frame x (nearer F/bustline) so that,
    # after mirroring flips x, leg_a ends up with the SMALLER local x --
    # nearer cf_waist, matching the "waist" edge's own cf_waist -> leg_a
    # -> ... -> leg_b wiring below -- keeps leg_a/leg_b in the same
    # left-to-right order the outline actually visits them in.
    s_x_shared = s.vertical_bust_line_s[0]
    leg_a_x_shared = s_x_shared + s.front_dart_intake / 2
    leg_b_x_shared = s_x_shared - s.front_dart_intake / 2
    leg_a = Point(
        "front_waist_dart_leg_a", *_to_mm(mirror((leg_a_x_shared, _bf_y(leg_a_x_shared, s.B, s.F)))),
    )
    leg_b = Point(
        "front_waist_dart_leg_b", *_to_mm(mirror((leg_b_x_shared, _bf_y(leg_b_x_shared, s.B, s.F)))),
    )
    waist_d1 = Point(
        "front_waist_dart_d1",
        *_dart_point_out((leg_a.x, leg_a.y), (leg_b.x, leg_b.y), (waist_apex.x, waist_apex.y)),
    )

    # Side seam: cut off directly, not folded as a dart — see the matching
    # back-piece comment.
    underarm_x_shared = s.underarm[0]
    side_waist_x_shared = underarm_x_shared + s.side_seam_front_share
    side_waist = Point(
        "side_waist_front",
        *_to_mm(mirror((side_waist_x_shared, _bf_y(side_waist_x_shared, s.B, s.F)))),
    )

    # front_neckline_c1/c2 were computed as (near K, near L) — the same
    # order as this edge (front_neck_side=K -> cf_neck=L), so no swap.
    neck_c1, neck_c2 = _to_mm(mirror(s.front_neckline_c1)), _to_mm(mirror(s.front_neckline_c2))

    armhole_b2 = Point("front_armhole_b2", *_to_mm(mirror(s.front_armhole_c1)))
    armhole_a2 = Point("front_armhole_a2", *_to_mm(mirror(s.front_armhole_c2)))
    full_armhole = _full_armhole_spline(s)[3:]  # A3-B2, B2-A2, A2-V -- see _full_armhole_spline
    armhole_segments = [
        Segment(sid, eid, SegmentKind.CUBIC, _to_mm(mirror(c1)), _to_mm(mirror(c2)))
        for sid, eid, (c1, c2, _end) in zip(
            ["underarm_front", "front_armhole_b2", "front_armhole_a2"],
            ["front_armhole_b2", "front_armhole_a2", "front_shoulder_tip"],
            full_armhole,
        )
    ]

    edges = [
        Edge(id="neckline", role=EdgeRole.NECKLINE, segments=[
            Segment("front_neck_side", "cf_neck", SegmentKind.CUBIC, neck_c1, neck_c2),
        ]),
        Edge(id="shoulder", role=EdgeRole.SEAM, segments=[
            Segment("front_shoulder_dart_leg_t", "front_shoulder_tip", SegmentKind.LINE),
        ]),
        Edge(id="armhole", role=EdgeRole.ARMHOLE, segments=armhole_segments),
        Edge(id="side_seam", role=EdgeRole.SEAM, segments=[
            Segment("underarm_front", "side_waist_front", SegmentKind.LINE),
        ]),
        Edge(id="waist", role=EdgeRole.SEAM, segments=[
            Segment("cf_waist", "front_waist_dart_leg_a", SegmentKind.LINE),
            Segment("front_waist_dart_leg_a", "front_waist_dart_d1", SegmentKind.LINE),
            Segment("front_waist_dart_d1", "front_waist_dart_leg_b", SegmentKind.LINE),
            Segment("front_waist_dart_leg_b", "side_waist_front", SegmentKind.LINE),
        ]),
        Edge(id="center_front", role=EdgeRole.SEAM, segments=[
            Segment("cf_neck", "cf_waist", SegmentKind.LINE),
        ]),
    ]

    return PatternPiece(
        id="bodice_front", name="Bodice front", cut_count=1, cut_mirrored=True,
        points=[
            cf_neck, front_neck_side, dart_leg_t, dart_apex, front_shoulder_tip, bust_point,
            vertical_bust_line_r1, vertical_bust_line_s,
            armhole_b2, armhole_a2, underarm, underarm_waist, cf_waist,
            waist_apex, leg_a, leg_b, waist_d1, side_waist,
        ],
        edges=edges,
        darts=[
            Dart(id="front_shoulder_dart", apex="front_shoulder_dart_apex",
                 leg_a="front_neck_side", leg_b="front_shoulder_dart_leg_t"),
            Dart(id="bust_dart", apex="front_waist_dart_apex",
                 leg_a="front_waist_dart_leg_a", leg_b="front_waist_dart_leg_b"),
        ],
        notches=[Notch(id="front_armhole_notch", point="underarm_front", count=2)],
        grainline=Grainline(start="cf_neck", end="cf_waist"),
    )


def draft_bodice(measurements: Measurements, intent: DesignIntent) -> list[PatternPiece]:
    """(Measurements, DesignIntent) -> [front, back]. Pure, no I/O.

    Generated, not correct — see module docstring.
    """
    shared = _shared_frame(measurements, intent)
    return [_draft_front(shared), _draft_back(shared)]


def draft_bodice_from_sloper(
    sloper_pieces: list[PatternPiece],
    sloper_measurements: Measurements,
    sloper_intent: DesignIntent,
    measurements: Measurements,
    intent: DesignIntent,
) -> list[PatternPiece]:
    """Draft a bodice starting from a fitted reference sloper (Phase 6)
    instead of the formulas alone. Still pure, no I/O — the caller looks
    up which pattern/version is the marked sloper (api.db, once that
    exists) and passes its already-loaded pieces in.

    `sloper_pieces` is a fitted garment's ACTUAL stored geometry — after
    whatever manual corrections a muslin fitting applied via Phase 5
    revisions, it generally won't exactly match what `draft_bodice()`
    would produce fresh for `sloper_measurements`/`sloper_intent`. That
    difference, per named point, IS the human correction a real fitting
    found and the formulas alone don't know about (CLAUDE.md's whole
    argument for why this project exists). Reusing the sloper's absolute
    coordinates wouldn't make sense for a different body, and reusing the
    plain formulas would throw the correction away — so this computes
    that per-point offset once, against the sloper's OWN inputs, and
    re-applies the identical offset on top of a fresh formula draft for
    the NEW inputs. A shoulder dart nudged 4mm deeper than the formula
    gave it stays nudged 4mm deeper, scaled implicitly by drafting the
    new body's own geometry first and only then re-applying the
    correction, rather than carried as a fixed absolute distance.

    A point that exists in the sloper but not in a fresh formula draft
    (hand-added by some later revision, with no formula baseline to
    offset from) is dropped — there's nothing principled to adapt it by
    yet. Flagged for Phase 6 to revisit once real revision history shows
    how often that actually happens; for now it's silently excluded
    rather than guessed at.
    """
    baseline = {p.id: p for p in draft_bodice(sloper_measurements, sloper_intent)}
    target = {p.id: p for p in draft_bodice(measurements, intent)}
    sloper = {p.id: p for p in sloper_pieces}

    adapted = []
    for piece_id, target_piece in target.items():
        sloper_piece = sloper.get(piece_id)
        baseline_piece = baseline.get(piece_id)
        if sloper_piece is None or baseline_piece is None:
            adapted.append(target_piece)  # no sloper data for this piece — formulas as-is
            continue
        adapted.append(_apply_sloper_correction(sloper_piece, baseline_piece, target_piece))
    return adapted


def _apply_sloper_correction(
    sloper_piece: PatternPiece, baseline_piece: PatternPiece, target_piece: PatternPiece,
) -> PatternPiece:
    sloper_pts = sloper_piece.point_map()
    baseline_pts = baseline_piece.point_map()
    corrected_points = []
    for target_pt in target_piece.points:
        sloper_pt = sloper_pts.get(target_pt.id)
        baseline_pt = baseline_pts.get(target_pt.id)
        if sloper_pt is None or baseline_pt is None:
            corrected_points.append(target_pt)  # new point this sloper predates — formula value as-is
            continue
        dx, dy = sloper_pt.x - baseline_pt.x, sloper_pt.y - baseline_pt.y
        corrected_points.append(Point(target_pt.id, target_pt.x + dx, target_pt.y + dy))
    return dataclasses.replace(target_piece, points=corrected_points)
