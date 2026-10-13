"""Builds a simplified bodice front (v1), applies a fit edit (v2), and shows the
diff + the text that would be embedded for RAG. Geometry is illustrative, not a
drafted block; the croquis step is what will generate real values."""
import copy
import sqlite3

from api.schema import (
    DDL, ChangeType, DesignIntent, Edge, EdgeRole, FabricClass, GarmentType,
    Grainline, Dart, Measurements, Notch, Pattern, PatternPiece, Point,
    Revision, RevisionSource, Segment, diff_patterns,
)


def build_v1() -> Pattern:
    pts = [
        Point("neck_side", 90, 0), Point("shoulder_tip", 190, 25),
        Point("armhole_depth", 205, 210), Point("waist_side", 215, 400),
        Point("waist_cf", 0, 400), Point("neck_cf", 0, 70),
        Point("bust_point", 100, 190),
        Point("dart_leg_a", 215, 175), Point("dart_leg_b", 215, 215),
        Point("grain_top", 60, 60), Point("grain_bot", 60, 360),
    ]
    edges = [
        Edge("shoulder", EdgeRole.SEAM,
             [Segment("neck_side", "shoulder_tip")], joins_to="bodice_back.shoulder"),
        Edge("armhole", EdgeRole.ARMHOLE,
             [Segment("shoulder_tip", "armhole_depth", "cubic", (215, 80), (195, 150))]),
        Edge("side_seam", EdgeRole.SEAM,
             [Segment("armhole_depth", "waist_side")], joins_to="bodice_back.side_seam"),
        Edge("waist", EdgeRole.SEAM, [Segment("waist_side", "waist_cf")]),
        Edge("centre_front", EdgeRole.FOLD, [Segment("waist_cf", "neck_cf")]),
        Edge("neckline", EdgeRole.NECKLINE,
             [Segment("neck_cf", "neck_side", "cubic", (0, 30), (40, 0))]),
    ]
    front = PatternPiece(
        id="bodice_front", name="Bodice front", cut_count=1, cut_mirrored=False,
        points=pts, edges=edges,
        darts=[Dart("bust_dart", "bust_point", "dart_leg_a", "dart_leg_b")],
        notches=[Notch("n_bust", "armhole_depth", 1)],
        grainline=Grainline("grain_top", "grain_bot"),
    )
    return Pattern(
        id="pat_001", name="Test bodice", version=1,
        measurements=Measurements(
            bust=920, waist=740, hip=980, shoulder_width=390,
            back_length=410, bust_height=250, bust_span=190),
        intent=DesignIntent(GarmentType.BODICE, FabricClass.WOVEN,
                            closures=["centre_back_zip"]),
        pieces=[front], croquis_ref="uploads/croquis_001.png",
    )


def apply_fit_edit(p: Pattern) -> Pattern:
    """Muslin showed pulling at the bust: widen the dart intake, raise the apex
    slightly, and increase seam allowance for more let-out room."""
    q = copy.deepcopy(p)
    q.version += 1
    f = q.piece("bodice_front")
    pm = f.point_map()
    pm["dart_leg_a"].y -= 5
    pm["dart_leg_b"].y += 5
    pm["bust_point"].y -= 8
    f.seam_allowance_mm = 20.0
    return q


if __name__ == "__main__":
    v1 = build_v1()
    v2 = apply_fit_edit(v1)

    deltas = diff_patterns(v1, v2)
    print("--- structural diff v1 -> v2 ---")
    for d in deltas:
        print(f"{d.path}: {d.old} -> {d.new}")

    rev = Revision(
        id="rev_001", pattern_id=v1.id, version=v2.version,
        parent_version=v1.version, change_type=ChangeType.DART,
        source=RevisionSource.MUSLIN_FITTING, deltas=deltas,
        reason="Widened bust dart and raised apex to remove pulling.",
        fit_notes="Drag lines pointing to bust point on first muslin.",
    )
    print("\n--- text that gets embedded ---")
    print(rev.embedding_text())

    # storage round-trip
    db = sqlite3.connect(":memory:")
    db.executescript(DDL)
    db.execute("INSERT INTO patterns VALUES (?,?,?,?)",
               (v1.id, v1.name, v2.version, rev.created_at))
    for v in (v1, v2):
        db.execute("INSERT INTO pattern_versions VALUES (?,?,?)",
                   (v.id, v.version, v.to_json()))
    print("\nstored versions:",
          db.execute("SELECT version FROM pattern_versions").fetchall())
    dart = v2.piece("bodice_front").darts[0]
    print("dart intake v1 -> v2 (mm):",
          v1.piece("bodice_front").darts[0].intake_mm(v1.piece("bodice_front").point_map()),
          "->", dart.intake_mm(v2.piece("bodice_front").point_map()))
