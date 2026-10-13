"""Tests for api.rag.embed — the local hashed bag-of-words embedder (see
CLAUDE.md's "Why local embeddings, not a semantic embeddings API").
"""
import sqlite3

from api.rag.embed import (
    DIMENSIONS, MODEL_NAME, backfill_missing_embeddings, embed_and_store_revision,
    embed_revision, embed_text, tokenize,
)
from api.schema import DDL, ChangeType, FieldDelta, Revision, RevisionSource


def _revision(id_="rev_1", reason="widened the back waist dart after it pulled", fit_notes="") -> Revision:
    return Revision(
        id=id_, pattern_id="pat_001", version=2, parent_version=1,
        change_type=ChangeType.DART, source=RevisionSource.MANUAL,
        deltas=[FieldDelta("pieces[bodice_back].darts[back_waist_dart].leg_a", "a", "b")],
        reason=reason, fit_notes=fit_notes,
    )


def _db_with_revision(revision: Revision) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    conn.execute(
        "INSERT INTO patterns (id, name, current_version, created_at) VALUES ('pat_001', 'x', 2, 'now')",
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
    conn.commit()
    return conn


def test_tokenize_lowercases_and_splits_underscored_and_dotted_identifiers():
    tokens = tokenize("Pieces[bodice_back].darts[back_waist_dart].leg_a: 15.0 -> 20.0")
    # the whole point of splitting on non-alphanumeric: an underscore_joined
    # id becomes its separate words, matching how a person would phrase a
    # question about it in plain English.
    assert "back" in tokens
    assert "waist" in tokens
    assert "dart" in tokens
    assert "back_waist_dart" not in tokens


def test_tokenize_drops_the_template_boilerplate_but_not_real_content():
    tokens = tokenize("dart change via manual. on pattern pat_001. Reason: widened the dart")
    for boilerplate in ("change", "via", "on", "pattern", "reason"):
        assert boilerplate not in tokens
    assert "dart" in tokens  # appears in real content too, not just the template skeleton
    assert "widened" in tokens


def test_embed_text_is_deterministic():
    a = embed_text("widened the back waist dart by 4mm")
    b = embed_text("widened the back waist dart by 4mm")
    assert list(a) == list(b)


def test_embed_text_is_l2_normalized_for_nonempty_text():
    vec = embed_text("widened the back waist dart")
    norm = sum(v * v for v in vec) ** 0.5
    assert abs(norm - 1.0) < 1e-5


def test_embed_text_of_all_stopword_text_is_the_zero_vector():
    vec = embed_text("change via on pattern reason fit notes")
    assert all(v == 0.0 for v in vec)
    assert len(vec) == DIMENSIONS


def test_embed_text_shares_more_signal_with_a_related_query_than_an_unrelated_one():
    """Not a claim of semantic understanding (see CLAUDE.md) -- just that
    shared vocabulary scores higher than no shared vocabulary, which is
    the entire mechanism this embedder relies on."""
    from api.rag.retrieve import cosine_similarity

    revision_text = "dart change via manual. Reason: widened the back waist dart after it pulled"
    related_query = embed_text("why did I widen the back waist dart")
    unrelated_query = embed_text("front neckline curve looked too low")
    revision_vec = embed_text(revision_text)

    assert cosine_similarity(revision_vec, related_query) > cosine_similarity(revision_vec, unrelated_query)


def test_embed_revision_uses_embedding_text():
    revision = _revision()
    assert list(embed_revision(revision)) == list(embed_text(revision.embedding_text()))


def test_embed_and_store_revision_round_trips_through_sqlite():
    revision = _revision()
    conn = _db_with_revision(revision)

    embed_and_store_revision(conn, revision)
    conn.commit()

    row = conn.execute(
        "SELECT model, vector FROM revision_embeddings WHERE revision_id = ?", (revision.id,),
    ).fetchone()
    assert row is not None
    model, vector_blob = row
    assert model == MODEL_NAME
    from array import array
    stored = array("f")
    stored.frombytes(vector_blob)
    assert list(stored) == list(embed_revision(revision))


def test_backfill_embeds_only_missing_revisions_and_is_idempotent():
    revision = _revision()
    conn = _db_with_revision(revision)  # revisions row exists, no embedding yet

    first_count = backfill_missing_embeddings(conn)
    assert first_count == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM revision_embeddings WHERE revision_id = ?", (revision.id,),
    ).fetchone() == (1,)

    second_count = backfill_missing_embeddings(conn)
    assert second_count == 0  # nothing left to embed
