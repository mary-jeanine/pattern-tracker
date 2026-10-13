"""Phase 0 smoke test: get_connection() bootstraps the schema."""
from api.db import get_connection


def test_get_connection_creates_schema():
    conn = get_connection(":memory:")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "patterns",
        "pattern_versions",
        "revisions",
        "revision_embeddings",
    } <= tables
