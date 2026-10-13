"""Cosine retrieval over a single pattern's revisions — brute-force in
Python, no vector-search library (CLAUDE.md is explicit: at this project's
scale, a few hundred rows, a linear scan beats importing one).
"""
from __future__ import annotations

import sqlite3
from array import array
from typing import Optional

from api.rag.embed import DIMENSIONS, embed_text
from api.schema import Revision, revision_from_row


def cosine_similarity(a: array, b: array) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def retrieve(
    db: sqlite3.Connection,
    pattern_id: str,
    query: str,
    top_k: int = 5,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> list[Revision]:
    """The pattern's own revisions, ranked by cosine similarity to `query`,
    highest first. Filters by `pattern_id` FIRST, in the SQL itself, before
    any ranking happens — cross-pattern bleed (another pattern's revision
    outscoring this pattern's own because it happened to share more words)
    is the obvious failure mode CLAUDE.md calls out, and it can't happen if
    the candidate set never includes another pattern's rows to begin with.

    `since`/`until` are ISO-8601 date/datetime strings compared directly
    against `created_at` — ISO-8601's lexical and chronological orders
    agree, so plain string comparison in SQL is correct without parsing.
    Either may be omitted.

    A revision with no stored embedding yet (shouldn't happen once
    embedding is wired into /revise, but a defensive case — see
    embed.backfill_missing_embeddings) is skipped rather than crashing or
    silently scoring it 0 alongside genuinely dissimilar revisions.
    """
    sql = (
        "SELECT r.id, r.pattern_id, r.version, r.parent_version, r.change_type, r.source, "
        "r.deltas_json, r.reason, r.fit_notes, r.created_at, e.vector "
        "FROM revisions r JOIN revision_embeddings e ON e.revision_id = r.id "
        "WHERE r.pattern_id = ?"
    )
    params: list = [pattern_id]
    if since is not None:
        sql += " AND r.created_at >= ?"
        params.append(since)
    if until is not None:
        sql += " AND r.created_at <= ?"
        params.append(until)

    rows = db.execute(sql, params).fetchall()
    if not rows:
        return []

    query_vec = embed_text(query)
    scored = []
    for row in rows:
        *revision_cols, vector_blob = row
        candidate_vec = array("f")
        candidate_vec.frombytes(vector_blob)
        if len(candidate_vec) != DIMENSIONS:
            continue  # a vector from a different (future) embedding model/dimension — not comparable
        scored.append((cosine_similarity(query_vec, candidate_vec), revision_cols))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [revision_from_row(tuple(cols)) for _score, cols in scored[:top_k]]
