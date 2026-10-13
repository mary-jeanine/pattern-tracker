"""Tests for infer_change_type."""
from api.schema import ChangeType, FieldDelta, infer_change_type


def test_dart_leg_path_classifies_as_dart():
    deltas = [FieldDelta("pieces[front].darts[bust_dart].apex", "a", "b")]
    assert infer_change_type(deltas) == ChangeType.DART


def test_dart_leg_point_classifies_as_dart():
    deltas = [FieldDelta("pieces[front].points[dart_leg_a].y", 175, 170)]
    assert infer_change_type(deltas) == ChangeType.DART


def test_seam_allowance_classifies_as_seam_allowance():
    deltas = [FieldDelta("pieces[front].seam_allowance_mm", 15.0, 20.0)]
    assert infer_change_type(deltas) == ChangeType.SEAM_ALLOWANCE


def test_ease_classifies_as_ease():
    deltas = [FieldDelta("intent.ease_bust_mm", 50.0, 60.0)]
    assert infer_change_type(deltas) == ChangeType.EASE


def test_dart_outranks_seam_allowance_in_the_same_revision():
    """The example_bodice.py case: a dart move that also nudges seam
    allowance should read as DART overall, not SEAM_ALLOWANCE."""
    deltas = [
        FieldDelta("pieces[front].points[dart_leg_a].y", 175, 170),
        FieldDelta("pieces[front].seam_allowance_mm", 15.0, 20.0),
    ]
    assert infer_change_type(deltas) == ChangeType.DART


def test_whole_piece_added():
    deltas = [FieldDelta("pieces[sleeve]", None, {"id": "sleeve"})]
    assert infer_change_type(deltas) == ChangeType.PIECE_ADDED


def test_whole_piece_removed():
    deltas = [FieldDelta("pieces[sleeve]", {"id": "sleeve"}, None)]
    assert infer_change_type(deltas) == ChangeType.PIECE_REMOVED


def test_segment_control_point_classifies_as_curve():
    deltas = [FieldDelta(
        "pieces[front].edges[armhole].segments[a->b].c1", [10, 10], [15, 12],
    )]
    assert infer_change_type(deltas) == ChangeType.CURVE


def test_measurement_change():
    deltas = [FieldDelta("measurements.bust", 900, 920)]
    assert infer_change_type(deltas) == ChangeType.MEASUREMENT


def test_single_axis_point_move_is_width_or_length():
    x_only = [FieldDelta("pieces[front].points[hem].x", 100, 110)]
    y_only = [FieldDelta("pieces[front].points[hem].y", 400, 420)]
    assert infer_change_type(x_only) == ChangeType.WIDTH
    assert infer_change_type(y_only) == ChangeType.LENGTH


def test_mixed_axis_point_moves_fall_back_to_other():
    """Low-confidence category: don't guess when axes disagree."""
    deltas = [
        FieldDelta("pieces[front].points[hem].x", 100, 110),
        FieldDelta("pieces[front].points[hem].y", 400, 420),
    ]
    assert infer_change_type(deltas) == ChangeType.OTHER


def test_empty_deltas_is_other():
    assert infer_change_type([]) == ChangeType.OTHER
