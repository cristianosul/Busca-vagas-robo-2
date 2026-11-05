import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.models import CandidateEvidence, CandidateIn
from app.scoring import score_candidate

DB_PATH = Path(os.getenv("DATABASE_PATH", "./data/staging.db"))
TRACKING_KEYS = {"fbclid", "gclid", "dclid", "msclkid", "ref", "referrer"}


def normalize_job_url(value: str) -> str:
    """Remove fragment e parâmetros de rastreamento sem descartar IDs da vaga."""
    parts = urlsplit(str(value).strip())
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    if not hostname:
        raise ValueError("URL original sem hostname válido")

    port = parts.port
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")

    query_items = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_KEYS:
            continue
        query_items.append((key, val))
    query = urlencode(sorted(query_items))
    return urlunsplit((scheme, netloc, path, query, ""))


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
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
                original_job_url TEXT NOT NULL,
                normalized_url TEXT,
                application_url TEXT,
                score INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('approved', 'review', 'rejected')),
                reasons_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)")}
        if "normalized_url" not in columns:
            conn.execute("ALTER TABLE candidates ADD COLUMN normalized_url TEXT")

        # Migração segura de uma base criada pela versão inicial.
        rows = conn.execute(
            "SELECT id, original_job_url FROM candidates WHERE normalized_url IS NULL OR normalized_url = '' ORDER BY id"
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            normalized = normalize_job_url(row["original_job_url"])
            if normalized in seen:
                normalized = f"{normalized}#legacy-duplicate-{row['id']}"
            seen.add(normalized)
            conn.execute("UPDATE candidates SET normalized_url = ? WHERE id = ?", (normalized, row["id"]))

        # Também registra URLs já normalizadas antes da criação do índice.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_candidates_normalized_url ON candidates(normalized_url)")


def save_candidate(candidate: CandidateIn, evidence: CandidateEvidence | None = None):
    evidence = evidence or CandidateEvidence()
    score, status, reasons = score_candidate(evidence)
    payload = candidate.model_dump(mode="json")
    created_at = datetime.now(timezone.utc).isoformat()
    original_url = str(candidate.original_job_url)
    normalized_url = normalize_job_url(original_url)

    # BEGIN IMMEDIATE serializa escritores: evita corrida entre consulta e inserção.
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id, status, score FROM candidates WHERE normalized_url = ?",
            (normalized_url,),
        ).fetchone()
        if existing:
            return {
                "id": existing["id"],
                "status": "duplicate",
                "existing_status": existing["status"],
                "score": existing["score"],
                "reasons": ["URL original já cadastrada após normalização; nenhum novo registro foi criado."],
            }

        cur = conn.execute("""
            INSERT INTO candidates (
                title, company, location, mode, source_name, source_url,
                original_job_url, normalized_url, application_url, score,
                status, reasons_json, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            candidate.title, candidate.company, candidate.location,
            candidate.mode, candidate.source_name, str(candidate.source_url),
            original_url, normalized_url,
            str(candidate.application_url) if candidate.application_url else None,
            score, status, json.dumps(reasons, ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False), created_at,
        ))
        return {"id": cur.lastrowid, "status": status, "score": score, "reasons": reasons}


def list_candidates(status: str | None = None, limit: int = 50):
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
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
        "created_at": row["created_at"],
    } for row in rows]
