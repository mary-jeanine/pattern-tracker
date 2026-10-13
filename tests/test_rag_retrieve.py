"""Tests for api.rag.retrieve — cosine ranking filtered to one pattern's
own revisions, no LLM or network call involved (see CLAUDE.md)."""
import sqlite3

from api.rag.embed import embed_and_store_revision
from api.rag.retrieve import cosine_similarity, retrieve
from api.schema import DDL, ChangeType, FieldDelta, Revision, RevisionSource


def _seed(conn: sqlite3.Connection, revision: Revision) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO patterns (id, name, current_version, created_at) "
        "VALUES (?, 'x', 1, 'now')",
        (revision.pattern_id,),
    )
    conn.execute(
        "INSERT INTO revisions (id, pattern_id, version, parent_version, change_type, source, "
        "deltas_json, reason, fit_notes, created_at) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?)",
        (
            revision.id, revision.pattern_id, revision.version, revision.parent_version,
            revision.change_type.value, revision.source.value,
            revision.reason, revision.fit_notes, revision.created_at,
        ),
    )
    embed_and_store_revision(conn, revision)
    conn.commit()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def _revision(id_, pattern_id="pat_001", reason="", created_at="2026-01-01T00:00:00+00:00") -> Revision:
    return Revision(
        id=id_, pattern_id=pattern_id, version=2, parent_version=1,
        change_type=ChangeType.DART, source=RevisionSource.MANUAL,
        deltas=[FieldDelta("pieces[bodice_back].darts[back_waist_dart].leg_a", "a", "b")],
        reason=reason, created_at=created_at,
    )


def test_cosine_similarity_of_identical_vectors_is_one():
    from array import array
    v = array("f", [1.0, 2.0, 3.0])
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-6


def test_cosine_similarity_of_a_zero_vector_is_zero_not_a_division_error():
    from array import array
    zero = array("f", [0.0, 0.0])
    other = array("f", [1.0, 1.0])
    assert cosine_similarity(zero, other) == 0.0


def test_retrieve_only_returns_the_requested_patterns_revisions():
    """Cross-pattern bleed: a revision on a DIFFERENT pattern that happens
    to share more vocabulary with the query must never outrank (or even
    appear alongside) the target pattern's own revisions."""
    conn = _db()
    _seed(conn, _revision("rev_a", pattern_id="pat_001", reason="widened the back waist dart"))
    _seed(conn, _revision("rev_b", pattern_id="pat_002", reason="widened the back waist dart even more"))

    results = retrieve(conn, "pat_001", "why did I widen the back waist dart")

    assert [r.id for r in results] == ["rev_a"]


def test_retrieve_ranks_the_more_similar_revision_first():
    conn = _db()
    _seed(conn, _revision("rev_relevant", reason="widened the back waist dart after it pulled"))
    _seed(conn, _revision("rev_irrelevant", reason="shortened the centre front hem"))

    results = retrieve(conn, "pat_001", "why did I widen the back waist dart")

    assert results[0].id == "rev_relevant"


def test_retrieve_respects_top_k():
    conn = _db()
    for i in range(3):
        _seed(conn, _revision(f"rev_{i}", reason=f"edit number {i}"))

    results = retrieve(conn, "pat_001", "edit", top_k=2)

    assert len(results) == 2


def test_retrieve_filters_by_date_range():
    conn = _db()
    _seed(conn, _revision("rev_old", reason="early edit", created_at="2026-01-01T00:00:00+00:00"))
    _seed(conn, _revision("rev_new", reason="late edit", created_at="2026-06-01T00:00:00+00:00"))

    results = retrieve(conn, "pat_001", "edit", since="2026-05-01", until="2026-12-31")

    assert [r.id for r in results] == ["rev_new"]


def test_retrieve_returns_an_empty_list_when_the_pattern_has_no_revisions():
    conn = _db()
    assert retrieve(conn, "pat_nonexistent", "anything") == []
