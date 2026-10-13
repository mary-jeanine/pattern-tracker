"""Retrieve, then synthesize: the only step in the RAG layer that calls an
LLM (CLAUDE.md — retrieval itself is a local cosine scan, no API call).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from anthropic import Anthropic
from pydantic import TypeAdapter

from api.rag.retrieve import retrieve
from api.schema import Revision

_VISION_MODEL = "claude-sonnet-5"  # same default as api.main's vision call; swap freely
_revision_adapter = TypeAdapter(Revision)

_SYSTEM_PROMPT = """\
You answer questions about the revision history of ONE sewing pattern, using \
only the revision records provided below — never anything outside them.

Each revision has structured `deltas` (the actual field-level changes, as \
old -> new pairs) plus free-text `reason` and `fit_notes` written by the \
person who made the edit.

Rules, and they matter more than being a complete answer:
- Quote measurements, point coordinates, and any other numbers ONLY from a \
delta's own `old`/`new` values. Never state a number that appears only in \
`reason` or `fit_notes` text as if it were an authoritative value — restate \
it as "the notes say..." instead, since notes are prose, not the record.
- If the provided revisions don't actually explain the answer to the \
question, say so plainly instead of guessing or inferring a reason that \
isn't written down anywhere in the data.
- Cite which revision(s) (by version number) you're drawing from.
"""


def _format_revision(revision: Revision) -> str:
    lines = [
        f"Revision v{revision.parent_version}->v{revision.version} "
        f"({revision.change_type.value}, via {revision.source.value}, {revision.created_at}):",
    ]
    for d in revision.deltas:
        lines.append(f"  - {d.path}: {d.old} -> {d.new}")
    if revision.reason:
        lines.append(f"  Reason: {revision.reason}")
    if revision.fit_notes:
        lines.append(f"  Fit notes: {revision.fit_notes}")
    return "\n".join(lines)


def build_prompt(question: str, revisions: list[Revision]) -> str:
    revision_block = "\n\n".join(_format_revision(r) for r in revisions)
    return f"Revisions:\n\n{revision_block}\n\nQuestion: {question}"


def answer_question(
    db: sqlite3.Connection,
    client: Anthropic,
    pattern_id: str,
    question: str,
    top_k: int = 5,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict[str, Any]:
    """Returns {"answer": str, "revisions_used": [...]} — the revisions are
    included, not just referenced by id, so the answer can be checked
    against the actual deltas without a second round trip (PLAN.md's own
    "Done when" for this phase)."""
    revisions = retrieve(db, pattern_id, question, top_k=top_k, since=since, until=until)

    if not revisions:
        return {
            "answer": "No revision history for this pattern (in the given date range, if one was set) "
                      "to answer from.",
            "revisions_used": [],
        }

    response = client.messages.create(
        model=_VISION_MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(question, revisions)}],
    )
    answer_text = response.content[0].text if response.content else ""

    return {
        "answer": answer_text,
        "revisions_used": [_revision_adapter.dump_python(r, mode="json") for r in revisions],
    }
