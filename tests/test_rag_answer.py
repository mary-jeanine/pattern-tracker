"""Tests for api.rag.answer — the one place in the RAG layer that calls an
LLM. Uses a fake Anthropic client (no network, no API key needed), the
same pattern tests/test_main.py uses for the croquis endpoint.
"""
import json
import sqlite3
from types import SimpleNamespace

from api.rag.answer import answer_question, build_prompt
from api.rag.embed import embed_and_store_revision
from api.schema import DDL, ChangeType, FieldDelta, Revision, RevisionSource


class _FakeAnthropicClient:
    def __init__(self, reply_text: str):
        self._reply_text = reply_text
        self.last_create_kwargs: dict | None = None

        class _Messages:
            def create(inner_self, **kwargs):
                self.last_create_kwargs = kwargs
                return SimpleNamespace(content=[SimpleNamespace(text=self._reply_text)])

        self.messages = _Messages()


def _revision() -> Revision:
    return Revision(
        id="rev_1", pattern_id="pat_001", version=2, parent_version=1,
        change_type=ChangeType.DART, source=RevisionSource.MANUAL,
        deltas=[FieldDelta("pieces[bodice_back].darts[back_waist_dart].intake_mm", 15.0, 19.0)],
        reason="widened the back waist dart after the first muslin pulled across the shoulder blade",
        fit_notes="diagonal pull released once let out 4mm",
    )


def _seeded_db(revision: Revision) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    conn.execute(
        "INSERT INTO patterns (id, name, current_version, created_at) VALUES ('pat_001', 'x', 2, 'now')",
    )
    deltas_json = json.dumps([{"path": d.path, "old": d.old, "new": d.new} for d in revision.deltas])
    conn.execute(
        "INSERT INTO revisions (id, pattern_id, version, parent_version, change_type, source, "
        "deltas_json, reason, fit_notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            revision.id, revision.pattern_id, revision.version, revision.parent_version,
            revision.change_type.value, revision.source.value, deltas_json,
            revision.reason, revision.fit_notes, revision.created_at,
        ),
    )
    embed_and_store_revision(conn, revision)
    conn.commit()
    return conn


def test_build_prompt_includes_deltas_reason_and_fit_notes():
    prompt = build_prompt("why did I change the back waist dart?", [_revision()])

    assert "15.0" in prompt
    assert "19.0" in prompt
    assert "pulled across the shoulder blade" in prompt
    assert "let out 4mm" in prompt
    assert "why did I change the back waist dart?" in prompt


def test_answer_question_returns_a_canned_response_without_calling_the_llm_when_nothing_matches():
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    conn.execute("INSERT INTO patterns (id, name, current_version, created_at) VALUES ('pat_001', 'x', 1, 'now')")
    conn.commit()
    fake_client = _FakeAnthropicClient("should never be returned")

    result = answer_question(conn, fake_client, "pat_001", "why did I change the bust dart?")

    assert result["revisions_used"] == []
    assert "No revision history" in result["answer"]
    assert fake_client.last_create_kwargs is None  # no LLM call was made


def test_answer_question_calls_the_llm_and_returns_the_revisions_it_used():
    revision = _revision()
    conn = _seeded_db(revision)
    fake_client = _FakeAnthropicClient("You widened the back waist dart by 4mm after the first muslin.")

    result = answer_question(conn, fake_client, "pat_001", "why did I change the back waist dart?")

    assert result["answer"] == "You widened the back waist dart by 4mm after the first muslin."
    assert len(result["revisions_used"]) == 1
    assert result["revisions_used"][0]["id"] == "rev_1"
    assert result["revisions_used"][0]["deltas"][0]["old"] == 15.0
    assert fake_client.last_create_kwargs is not None
    assert "back waist dart" in fake_client.last_create_kwargs["messages"][0]["content"]
