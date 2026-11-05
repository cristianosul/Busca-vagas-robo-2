import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from app.models import CandidateIn, CandidateVerification
from app.scoring import score_candidate

DB_PATH = Path(os.getenv("DATABASE_PATH", "./data/staging.db"))


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT,
                mode TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                original_job_url TEXT NOT NULL UNIQUE,
                application_url TEXT,
                score INTEGER NOT NULL,
                status TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


def save_candidate(
    candidate: CandidateIn,
    verification: CandidateVerification | None = None,
):
    score, status, reasons = score_candidate(
    candidate,
    verification,
    )
    payload = candidate.model_dump(mode="json")
    created_at = datetime.now(timezone.utc).isoformat()

    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM candidates WHERE original_job_url = ?",
            (str(candidate.original_job_url),)
        ).fetchone()

        if existing:
            return {"id": existing["id"], "status": "duplicate", "score": score,
                    "reasons": ["URL original já cadastrada; nenhum novo registro foi criado."]}

        cur = conn.execute("""
            INSERT INTO candidates (
                title, company, location, mode, source_name, source_url,
                original_job_url, application_url, score, status,
                reasons_json, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            candidate.title, candidate.company, candidate.location,
            candidate.mode, candidate.source_name, str(candidate.source_url),
            str(candidate.original_job_url),
            str(candidate.application_url) if candidate.application_url else None,
            score, status, json.dumps(reasons, ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False), created_at
        ))
        return {"id": cur.lastrowid, "status": status, "score": score, "reasons": reasons}


def list_candidates(status: str | None = None, limit: int = 50):
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM candidates ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    return [{
        "id": row["id"], "title": row["title"], "company": row["company"],
        "location": row["location"], "mode": row["mode"],
        "source_name": row["source_name"],
        "original_job_url": row["original_job_url"],
        "application_url": row["application_url"],
        "score": row["score"], "status": row["status"],
        "reasons": json.loads(row["reasons_json"]),
        "created_at": row["created_at"]
    } for row in rows]
