"""Integration test for the Phase 3 PDF endpoint, the Phase 5 mark-sloper
endpoint, the Phase 4 sloper lookup, and the Phase 4 croquis endpoint:
seeds a real in-memory SQLite DB with a pattern version (reusing the
Phase 2 bodice drafter), then hits the actual FastAPI routes rather than
calling the underlying functions directly — this is the first thing
that exercises api/db.py, api/schema.py's pattern_from_json, and
api/export/pdf.py wired together as the endpoints will really run.

The croquis tests never touch the real Anthropic API or need a real key
— `get_anthropic_client` is overridden with `_FakeAnthropicClient`,
a minimal stand-in whose `.messages.create()` returns a canned response
shaped just enough like the real SDK's (`.content[0].text`) for
api/main.py's own code to read.
"""
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api.drafting.bodice import draft_bodice
from api.main import app, get_anthropic_client, get_db
from api.schema import DDL, DesignIntent, FabricClass, GarmentType, Measurements, Pattern


class _FakeAnthropicClient:
    """Stands in for anthropic.Anthropic in tests: records the call it
    received and returns whatever text the test configured, without any
    network access or API key."""
    def __init__(self, reply_text: str):
        self._reply_text = reply_text
        self.last_create_kwargs: dict | None = None

        class _Messages:
            def create(inner_self, **kwargs):
                self.last_create_kwargs = kwargs
                return SimpleNamespace(content=[SimpleNamespace(text=self._reply_text)])

        self.messages = _Messages()


def _seeded_db():
    # FastAPI runs sync routes in a worker thread; a connection opened on the
    # test's own thread needs check_same_thread=False to be usable there.
    # get_connection() doesn't expose that (production opens one connection
    # per request, naturally thread-local), so this test builds its own.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(DDL)
    measurements = Measurements(
        bust=920, waist=740, hip=980, shoulder_width=380,
        back_length=400, bust_height=240, bust_span=180,
    )
    intent = DesignIntent(GarmentType.BODICE, FabricClass.WOVEN)
    pattern = Pattern(
        id="pat_001", name="Test bodice", version=1,
        measurements=measurements, intent=intent,
        pieces=draft_bodice(measurements, intent),
    )
    conn.execute(
        "INSERT INTO patterns (id, name, current_version, created_at) VALUES (?, ?, ?, datetime('now'))",
        (pattern.id, pattern.name, pattern.version),
    )
    conn.execute(
        "INSERT INTO pattern_versions (pattern_id, version, snapshot_json) VALUES (?, ?, ?)",
        (pattern.id, pattern.version, pattern.to_json()),
    )
    conn.commit()
    return conn, pattern


def _client() -> TestClient:
    client, _conn, _pattern = _client_with_db()
    return client


def _client_with_db() -> tuple[TestClient, sqlite3.Connection, Pattern]:
    conn, pattern = _seeded_db()

    def override_get_db():
        yield conn

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), conn, pattern


def _client_with_vision(reply_text: str) -> tuple[TestClient, _FakeAnthropicClient]:
    fake_client = _FakeAnthropicClient(reply_text)

    def override_get_anthropic_client():
        return fake_client

    app.dependency_overrides[get_anthropic_client] = override_get_anthropic_client
    return TestClient(app), fake_client


def _client_with_db_and_vision(reply_text: str) -> tuple[TestClient, sqlite3.Connection, Pattern, _FakeAnthropicClient]:
    client, conn, pattern = _client_with_db()
    fake_client = _FakeAnthropicClient(reply_text)

    def override_get_anthropic_client():
        return fake_client

    app.dependency_overrides[get_anthropic_client] = override_get_anthropic_client
    return client, conn, pattern, fake_client


def test_pdf_endpoint_returns_pdf_for_an_existing_version():
    client = _client()
    response = client.get("/patterns/pat_001/versions/1/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")


def test_pdf_endpoint_404s_for_a_missing_version():
    client = _client()
    response = client.get("/patterns/pat_001/versions/99/pdf")

    assert response.status_code == 404


def test_pdf_endpoint_404s_for_a_missing_pattern():
    client = _client()
    response = client.get("/patterns/does_not_exist/versions/1/pdf")

    assert response.status_code == 404


def test_revise_creates_a_new_version_and_a_revision_row():
    client, conn, pattern = _client_with_db()
    revised = pattern.to_dict()
    revised["pieces"][0]["seam_allowance_mm"] = 20.0

    response = client.post(
        "/patterns/pat_001/revise",
        json={"pattern": revised, "reason": "wider seam allowance for a heavier fabric"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 2
    assert body["parent_version"] == 1
    assert body["change_type"] == "seam_allowance"
    assert body["source"] == "manual"
    assert body["reason"] == "wider seam allowance for a heavier fabric"
    assert len(body["deltas"]) == 1
    assert "seam_allowance_mm" in body["deltas"][0]["path"]

    assert conn.execute("SELECT current_version FROM patterns WHERE id = 'pat_001'").fetchone() == (2,)
    assert conn.execute(
        "SELECT version FROM pattern_versions WHERE pattern_id = 'pat_001' ORDER BY version",
    ).fetchall() == [(1,), (2,)]
    assert conn.execute("SELECT COUNT(*) FROM revisions WHERE pattern_id = 'pat_001'").fetchone() == (1,)


def test_revise_rejects_an_empty_diff():
    client, _conn, pattern = _client_with_db()

    response = client.post(
        "/patterns/pat_001/revise",
        json={"pattern": pattern.to_dict(), "reason": "nothing actually changed"},
    )

    assert response.status_code == 400


def test_revise_rejects_a_blank_reason():
    client, _conn, pattern = _client_with_db()
    revised = pattern.to_dict()
    revised["pieces"][0]["seam_allowance_mm"] = 20.0

    response = client.post("/patterns/pat_001/revise", json={"pattern": revised, "reason": "   "})

    assert response.status_code == 400


def test_revise_404s_for_a_missing_pattern():
    client, _conn, pattern = _client_with_db()

    response = client.post(
        "/patterns/does_not_exist/revise",
        json={"pattern": pattern.to_dict(), "reason": "doesn't matter"},
    )

    assert response.status_code == 404


def test_get_revisions_returns_newest_first():
    client, _conn, pattern = _client_with_db()
    first = pattern.to_dict()
    first["pieces"][0]["seam_allowance_mm"] = 20.0
    client.post("/patterns/pat_001/revise", json={"pattern": first, "reason": "first bump"})
    second = first.copy()
    second["pieces"] = [dict(p) for p in first["pieces"]]
    second["pieces"][0]["seam_allowance_mm"] = 25.0
    client.post("/patterns/pat_001/revise", json={"pattern": second, "reason": "second bump"})

    response = client.get("/patterns/pat_001/revisions")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert [r["version"] for r in body] == [3, 2]  # newest first
    assert body[0]["reason"] == "second bump"
    assert body[1]["reason"] == "first bump"


def test_get_revisions_is_an_empty_list_for_a_pattern_with_no_revisions_yet():
    client, _conn, _pattern = _client_with_db()

    response = client.get("/patterns/pat_001/revisions")

    assert response.status_code == 200
    assert response.json() == []


def test_get_revisions_404s_for_a_missing_pattern():
    client = _client()
    response = client.get("/patterns/does_not_exist/revisions")

    assert response.status_code == 404


def test_diff_aggregates_directly_between_snapshots_not_per_revision():
    """A field nudged across two revisions (15mm -> 20mm -> 25mm) should
    show up in a from=1,to=3 diff as the one end-to-end change (15 -> 25),
    not two redundant entries concatenated from each revision's own
    stored deltas."""
    client, _conn, pattern = _client_with_db()
    first = pattern.to_dict()
    first["pieces"][0]["seam_allowance_mm"] = 20.0
    client.post("/patterns/pat_001/revise", json={"pattern": first, "reason": "first bump"})
    second = first.copy()
    second["pieces"] = [dict(p) for p in first["pieces"]]
    second["pieces"][0]["seam_allowance_mm"] = 25.0
    client.post("/patterns/pat_001/revise", json={"pattern": second, "reason": "second bump"})

    response = client.get("/patterns/pat_001/diff", params={"from": 1, "to": 3})

    assert response.status_code == 200
    body = response.json()
    assert body["from"] == 1
    assert body["to"] == 3
    seam_deltas = [d for d in body["deltas"] if "seam_allowance_mm" in d["path"]]
    assert len(seam_deltas) == 1
    assert seam_deltas[0]["old"] == 15.0
    assert seam_deltas[0]["new"] == 25.0


def test_diff_404s_for_a_missing_version():
    client, _conn, _pattern = _client_with_db()

    response = client.get("/patterns/pat_001/diff", params={"from": 1, "to": 99})

    assert response.status_code == 404


def test_ask_endpoint_answers_from_a_revision_made_through_this_api():
    """End-to-end: a real revision created via POST /revise (which embeds
    it automatically) is retrievable and answerable via POST /ask — not a
    hand-seeded row, the actual path a real revision takes."""
    client, _conn, pattern, fake_client = _client_with_db_and_vision(
        "You widened the back waist dart by 5mm after the first muslin pulled.",
    )
    revised = pattern.to_dict()
    revised["pieces"][0]["seam_allowance_mm"] = 20.0
    client.post(
        "/patterns/pat_001/revise",
        json={"pattern": revised, "reason": "wider seam allowance for a heavier fabric"},
    )

    response = client.post("/patterns/pat_001/ask", json={"question": "why did the seam allowance change?"})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "You widened the back waist dart by 5mm after the first muslin pulled."
    assert len(body["revisions_used"]) == 1
    assert body["revisions_used"][0]["reason"] == "wider seam allowance for a heavier fabric"
    assert fake_client.last_create_kwargs is not None


def test_ask_endpoint_404s_for_a_missing_pattern():
    client, _conn, _pattern, _fake = _client_with_db_and_vision("irrelevant")

    response = client.post("/patterns/does_not_exist/ask", json={"question": "anything"})

    assert response.status_code == 404


def test_ask_endpoint_does_not_call_the_llm_for_a_pattern_with_no_revisions():
    client, _conn, _pattern, fake_client = _client_with_db_and_vision("should never be returned")

    response = client.post("/patterns/pat_001/ask", json={"question": "anything"})

    assert response.status_code == 200
    assert response.json()["revisions_used"] == []
    assert fake_client.last_create_kwargs is None


def test_mark_sloper_marks_the_version_under_its_own_garment_type():
    client, conn, _pattern = _client_with_db()

    response = client.post("/patterns/pat_001/versions/1/mark-sloper")

    assert response.status_code == 200
    body = response.json()
    assert body["garment_type"] == "bodice"
    assert body["pattern_id"] == "pat_001"
    assert body["version"] == 1
    assert body["marked_at"]  # non-empty timestamp

    row = conn.execute(
        "SELECT garment_type, pattern_id, version FROM reference_slopers WHERE garment_type = 'bodice'",
    ).fetchone()
    assert row == ("bodice", "pat_001", 1)


def test_mark_sloper_replaces_the_previous_sloper_for_the_same_garment_type():
    """One sloper per garment_type (schema.py's PRIMARY KEY on garment_type)
    — marking a second version for the same type replaces the first, it
    doesn't add a second candidate row."""
    client, conn, _pattern = _client_with_db()
    conn.execute(
        "INSERT INTO pattern_versions (pattern_id, version, snapshot_json) "
        "SELECT pattern_id, 2, snapshot_json FROM pattern_versions WHERE pattern_id = 'pat_001' AND version = 1",
    )
    conn.commit()

    client.post("/patterns/pat_001/versions/1/mark-sloper")
    response = client.post("/patterns/pat_001/versions/2/mark-sloper")

    assert response.status_code == 200
    rows = conn.execute("SELECT version FROM reference_slopers WHERE garment_type = 'bodice'").fetchall()
    assert rows == [(2,)]


def test_mark_sloper_404s_for_a_missing_version():
    client = _client()
    response = client.post("/patterns/pat_001/versions/99/mark-sloper")

    assert response.status_code == 404


def test_get_sloper_returns_the_marked_patterns_pieces_measurements_and_intent():
    """Phase 4's actual use case: everything draft_bodice_from_sloper needs
    (sloper_pieces, sloper_measurements, sloper_intent) comes back in one
    round trip, not just a pointer to go resolve."""
    client, _conn, _pattern = _client_with_db()
    client.post("/patterns/pat_001/versions/1/mark-sloper")

    response = client.get("/slopers/bodice")

    assert response.status_code == 200
    body = response.json()
    assert body["garment_type"] == "bodice"
    assert body["pattern_id"] == "pat_001"
    assert body["version"] == 1
    assert body["marked_at"]
    assert body["pattern"]["measurements"]["bust"] == 920
    assert body["pattern"]["intent"]["garment_type"] == "bodice"
    piece_ids = {p["id"] for p in body["pattern"]["pieces"]}
    assert piece_ids == {"bodice_front", "bodice_back"}


def test_get_sloper_404s_when_none_marked_for_that_garment_type():
    client, _conn, _pattern = _client_with_db()  # seeded, but mark-sloper never called

    response = client.get("/slopers/bodice")

    assert response.status_code == 404


def test_get_sloper_422s_for_an_unrecognised_garment_type():
    client = _client()
    response = client.get("/slopers/not_a_real_garment_type")

    assert response.status_code == 422


# --------------------------------------------------------------------------
# POST /croquis (Phase 4 sketch) — isolates CROQUIS_DIR to a pytest tmp_path
# so tests never write into the real repo's croquis/ folder.
# --------------------------------------------------------------------------
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)  # smallest possible valid 1x1 PNG, not a real sketch -- just needs to be non-empty bytes


def test_croquis_endpoint_stores_the_image_and_returns_a_parsed_intent(tmp_path, monkeypatch):
    import api.main as main_module
    monkeypatch.setattr(main_module, "CROQUIS_DIR", tmp_path / "croquis")

    reply = (
        '{"garment_type": "dress", "fabric_class": "knit", "fit": "relaxed", '
        '"closures": ["centre_back_zip"], "style_notes": "boat neck, cap sleeves"}'
    )
    client, fake = _client_with_vision(reply)

    response = client.post(
        "/croquis", files={"file": ("sketch.png", _PNG_BYTES, "image/png")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["parse_error"] is None
    assert body["intent"]["garment_type"] == "dress"
    assert body["intent"]["fabric_class"] == "knit"
    assert body["intent"]["fit"] == "relaxed"
    assert body["intent"]["closures"] == ["centre_back_zip"]
    assert body["intent"]["style_notes"] == "boat neck, cap sleeves"
    # untouched defaults the vision prompt was told to leave out
    assert body["intent"]["ease_bust_mm"] == 50.0

    stored = (tmp_path / "croquis").parent / body["croquis_ref"]
    assert stored.exists()
    assert stored.read_bytes() == _PNG_BYTES

    # the image was actually sent to the vision call, not just stored
    sent_content = fake.last_create_kwargs["messages"][0]["content"]
    image_block = next(b for b in sent_content if b["type"] == "image")
    assert image_block["source"]["media_type"] == "image/png"


def test_croquis_endpoint_strips_markdown_fences_before_parsing(tmp_path, monkeypatch):
    import api.main as main_module
    monkeypatch.setattr(main_module, "CROQUIS_DIR", tmp_path / "croquis")

    fenced_reply = '```json\n{"garment_type": "skirt"}\n```'
    client, _fake = _client_with_vision(fenced_reply)

    response = client.post(
        "/croquis", files={"file": ("sketch.png", _PNG_BYTES, "image/png")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["parse_error"] is None
    assert body["intent"]["garment_type"] == "skirt"


def test_croquis_endpoint_returns_raw_text_on_a_parse_failure_instead_of_erroring(tmp_path, monkeypatch):
    """PLAN.md Phase 4: on a parse failure, hand back the raw text for
    manual correction rather than retrying blindly or failing the whole
    request -- the image was still stored successfully."""
    import api.main as main_module
    monkeypatch.setattr(main_module, "CROQUIS_DIR", tmp_path / "croquis")

    client, _fake = _client_with_vision("Sorry, I can't quite tell what garment this is.")

    response = client.post(
        "/croquis", files={"file": ("sketch.png", _PNG_BYTES, "image/png")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["intent"] is None
    assert body["parse_error"]
    assert "can't quite tell" in body["raw_response"]
    # the image was still stored despite the parse failure
    assert (tmp_path / "croquis").exists()
    assert len(list((tmp_path / "croquis").iterdir())) == 1


def test_croquis_endpoint_returns_raw_text_when_json_is_valid_but_not_a_design_intent(tmp_path, monkeypatch):
    """Valid JSON that doesn't match DesignIntent's shape (e.g. missing the
    required garment_type) is exactly as much a parse failure as
    non-JSON text -- caught by the same path, not a 500."""
    import api.main as main_module
    monkeypatch.setattr(main_module, "CROQUIS_DIR", tmp_path / "croquis")

    client, _fake = _client_with_vision('{"not_a_real_field": true}')

    response = client.post(
        "/croquis", files={"file": ("sketch.png", _PNG_BYTES, "image/png")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["intent"] is None
    assert body["parse_error"]


def test_croquis_endpoint_400s_for_an_empty_file(tmp_path, monkeypatch):
    import api.main as main_module
    monkeypatch.setattr(main_module, "CROQUIS_DIR", tmp_path / "croquis")

    client, _fake = _client_with_vision("{}")
    response = client.post("/croquis", files={"file": ("sketch.png", b"", "image/png")})

    assert response.status_code == 400
