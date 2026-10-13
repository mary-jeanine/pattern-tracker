"""SQLite connection helper.

Executes `schema.DDL` on every connection. That's cheap and safe because
every statement in DDL is `CREATE TABLE IF NOT EXISTS` — on an existing
database it's a no-op; on a fresh one it bootstraps the schema. No separate
migration step for Phase 0.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from dotenv import load_dotenv

from api.schema import DDL

# Loaded here since this is currently the first module most entry points
# (tests, scripts, eventually main.py) import. Only ANTHROPIC_API_KEY lives
# in .env today (see CLAUDE.md); db.py itself doesn't read it, but nothing
# downstream has to remember to call this themselves. A no-op if .env is
# absent, so it's harmless in CI or a fresh checkout.
load_dotenv()

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "pattern_tracker.db"


def get_connection(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with the schema ensured.

    Pass `path=":memory:"` for tests or throwaway scripts.
    """
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    return conn
