"""SQLite session history for persistent review tracking."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


DB_PATH = Path(__file__).parent.parent / "council_history.db"


class HistoryStore:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    request TEXT NOT NULL,
                    file_paths TEXT,
                    num_suggestions INTEGER DEFAULT 0,
                    num_agents INTEGER DEFAULT 0,
                    total_in_tokens INTEGER DEFAULT 0,
                    total_out_tokens INTEGER DEFAULT 0,
                    elapsed_seconds REAL DEFAULT 0,
                    report TEXT
                )
            """)

    def save(
        self,
        request: str,
        file_paths: str,
        num_suggestions: int,
        num_agents: int,
        total_in: int,
        total_out: int,
        elapsed: float,
        report: str,
    ) -> int:
        """Save session and return its ID."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO sessions VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now().isoformat(),
                    request,
                    file_paths,
                    num_suggestions,
                    num_agents,
                    total_in,
                    total_out,
                    round(elapsed, 1),
                    report,
                ),
            )
            return cur.lastrowid or 0

    def list_recent(self, limit: int = 10) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, timestamp, substr(request, 1, 100) as request, "
                "num_suggestions, num_agents, total_in_tokens, total_out_tokens, "
                "elapsed_seconds "
                "FROM sessions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_session(self, session_id: int) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row else None
