"""Persistent repository for Busca Vagas — Robô 2.

The repository keeps raw discoveries, extraction work, intermediate extracted
records and manually reviewed candidates separate. It performs no network
requests and never enables sources or publishes opportunities.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.models import (
    CandidateEvidence,
    CandidateIn,
    ExtractedJobData,
    EvidenceRecord,
    SourceCreate,
)
from app.scoring import score_candidate


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = Path(os.getenv("DATABASE_PATH", "./data/staging.db"))

# Identificador fixo para serializar as migrações no PostgreSQL.
MIGRATION_LOCK_ID = 731202611

TRACKING_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "ref", "referrer"
}

CANDIDATE_STATUSES = {"approved", "review", "rejected"}
EXTRACTION_STATUSES = {"pending", "running", "completed", "failed"}
EXTRACTED_JOB_STATUSES = {"incomplete", "complete"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_integer(value, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} deve ser um inteiro positivo.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} não pode ultrapassar {maximum}.")
    return value


def normalize_job_url(value: str) -> str:
    """Valida e normaliza URLs, removendo rastreadores e fragmentos."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("URL original inválida.")

    value = value.strip()
    if any(
        char.isspace() or ord(char) < 32 or ord(char) == 127
        for char in value
    ):
        raise ValueError(
            "URL original contém espaços ou caracteres de controle."
        )
    if "\\" in value:
        raise ValueError("URL original contém barras inválidas.")

    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        hostname = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as exc:
        raise ValueError("URL original malformada.") from exc

    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("URL original sem hostname válido.")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL original não pode conter credenciais.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Porta inválida na URL original.")

    if ":" in hostname:
        if not parts.netloc.startswith("["):
            raise ValueError("Endereço IPv6 inválido na URL original.")
        hostname = f"[{hostname}]"
    else:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError(
                "Hostname inválido na URL original."
            ) from exc

        if len(hostname) > 253:
            raise ValueError("Hostname excede o tamanho permitido.")

        labels = hostname.rstrip(".").split(".")
        if not labels or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(char.isalnum() or char == "-" for char in label)
            for label in labels
        ):
            raise ValueError("Hostname inválido na URL original.")

    if port is not None and not (
        (scheme == "https" and port == 443)
        or (scheme == "http" and port == 80)
    ):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"

    query_items = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_KEYS:
            continue
        query_items.append((key, val))

    query = urlencode(sorted(query_items))
    return urlunsplit((scheme, netloc, path, query, ""))


def _convert_sql_placeholders(sql: str) -> str:
    """Converte placeholders ? para %s sem alterar literais ou comentários."""
    result = []
    index = 0
    length = len(sql)
    state = "normal"
    dollar_delimiter = None
    block_comment_depth = 0

    while index < length:
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < length else ""

        if state == "normal":
            if char == "'":
                result.append(char)
                state = "single_quote"
                index += 1
                continue
            if char == '"':
                result.append(char)
                state = "double_quote"
                index += 1
                continue
            if char == "-" and next_char == "-":
                result.append("--")
                state = "line_comment"
                index += 2
                continue
            if char == "/" and next_char == "*":
                result.append("/*")
                state = "block_comment"
                block_comment_depth = 1
                index += 2
                continue
            if char == "$":
                match = re.match(
                    r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$",
                    sql[index:],
                )
                if match:
                    dollar_delimiter = match.group(0)
                    result.append(dollar_delimiter)
                    state = "dollar_quote"
                    index += len(dollar_delimiter)
                    continue
            result.append("%s" if char == "?" else char)
            index += 1
            continue

        if state == "single_quote":
            result.append(char)
            if char == "'" and next_char == "'":
                result.append(next_char)
                index += 2
                continue
            if char == "'":
                state = "normal"
            index += 1
            continue

        if state == "double_quote":
            result.append(char)
            if char == '"' and next_char == '"':
                result.append(next_char)
                index += 2
                continue
            if char == '"':
                state = "normal"
            index += 1
            continue

        if state == "line_comment":
            result.append(char)
            if char == "\n":
                state = "normal"
            index += 1
            continue

        if state == "block_comment":
            if char == "/" and next_char == "*":
                result.append("/*")
                block_comment_depth += 1
                index += 2
                continue
            if char == "*" and next_char == "/":
                result.append("*/")
                block_comment_depth -= 1
                index += 2
                if block_comment_depth == 0:
                    state = "normal"
                continue
            result.append(char)
            index += 1
            continue

        if state == "dollar_quote":
            if sql.startswith(dollar_delimiter, index):
                result.append(dollar_delimiter)
                index += len(dollar_delimiter)
                dollar_delimiter = None
                state = "normal"
                continue
            result.append(char)
            index += 1
            continue

    return "".join(result)


def connect():
    """Conecta ao PostgreSQL configurado ou ao SQLite local."""
    if DATABASE_URL:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "Driver PostgreSQL ausente. Instale psycopg[binary]."
            ) from exc
        return psycopg.connect(
            DATABASE_URL,
            sslmode="require",
            connect_timeout=10,
            row_factory=dict_row,
        )

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _validate_timeout_ms(value, name: str):
    if value is None:
        return None
    return _positive_integer(value, name)


@contextmanager
def db_session(
    immediate: bool = False,
    *,
    statement_timeout_ms: int | None = None,
    lock_timeout_ms: int | None = None,
):
    """Garante transação e fechamento; limites PostgreSQL são locais à transação."""
    statement_timeout_ms = _validate_timeout_ms(
        statement_timeout_ms, "statement_timeout_ms"
    )
    lock_timeout_ms = _validate_timeout_ms(lock_timeout_ms, "lock_timeout_ms")
    conn = connect()
    try:
        if not DATABASE_URL and immediate:
            conn.execute("BEGIN IMMEDIATE")
        with conn:
            if DATABASE_URL:
                if statement_timeout_ms is not None:
                    conn.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (f"{statement_timeout_ms}ms",),
                    )
                if lock_timeout_ms is not None:
                    conn.execute(
                        "SELECT set_config('lock_timeout', %s, true)",
                        (f"{lock_timeout_ms}ms",),
                    )
            yield conn
    finally:
        conn.close()


def execute(conn, sql, params=()):
    """Compatibiliza placeholders SQLite e PostgreSQL."""
    if DATABASE_URL:
        sql = _convert_sql_placeholders(sql)
    return conn.execute(sql, params)


def _get_table_columns(conn, table_name: str) -> set[str]:
    if table_name not in {"candidates", "sources"}:
        raise ValueError("Tabela não permitida para inspeção.")
    if DATABASE_URL:
        rows = execute(
            conn,
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = ?
            """,
            (table_name,),
        ).fetchall()
        return {row["column_name"] for row in rows}
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _ensure_column(
    conn,
    table_name: str,
    column_name: str,
    column_definition: str,
):
    allowed_columns = {
        "candidates": {"normalized_url", "evidence_json"},
        "sources": {"official_site_url", "authorization_evidence_url"},
    }
    if column_name not in allowed_columns.get(table_name, set()):
        raise ValueError("Coluna não permitida para migração.")
    if column_name in _get_table_columns(conn, table_name):
        return
    if DATABASE_URL:
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS "
            f"{column_name} {column_definition}"
        )
    else:
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN "
            f"{column_name} {column_definition}"
        )


def _json_dumps(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_load(value, *, context: str):
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"JSON inválido ou corrompido: {context}.") from exc


def serialize_source(row):
    if row is None:
        return None
    result = dict(row)
    result["enabled"] = bool(result["enabled"])
    return result


def _serialize_discovery_payload(result) -> str:
    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json")
    elif is_dataclass(result) and not isinstance(result, type):
        payload = asdict(result)
    elif isinstance(result, dict):
        payload = result
    elif hasattr(result, "__dict__"):
        payload = vars(result)
    else:
        raise TypeError("Formato de resultado de descoberta não suportado.")
    try:
        return _json_dumps(payload)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError(
            "Não foi possível serializar o resultado bruto."
        ) from exc


def _create_candidate_and_source_tables(conn):
    if DATABASE_URL:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candidates (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
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
                status TEXT NOT NULL
                    CHECK(status IN ('approved', 'review', 'rejected')),
                reasons_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                base_url TEXT NOT NULL,
                official_site_url TEXT,
                authorization_evidence_url TEXT,
                "authorization" TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
    else:
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
                status TEXT NOT NULL
                    CHECK(status IN ('approved', 'review', 'rejected')),
                reasons_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                base_url TEXT NOT NULL,
                official_site_url TEXT,
                authorization_evidence_url TEXT,
                "authorization" TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def _create_discovery_tables(conn):
    if DATABASE_URL:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discovery_results (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                municipality_code TEXT NOT NULL,
                municipality_name TEXT NOT NULL,
                state TEXT NOT NULL,
                category TEXT NOT NULL,
                query TEXT NOT NULL,
                normalized_url TEXT NOT NULL,
                result_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE (municipality_code, category, query, normalized_url)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discovery_result_history (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                discovery_result_id BIGINT NOT NULL,
                previous_result_json TEXT NOT NULL,
                previous_last_seen_at TEXT NOT NULL,
                archived_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS extraction_queue (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                discovery_result_id BIGINT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'running', 'completed', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts >= 1),
                available_at TEXT NOT NULL,
                lease_token TEXT,
                lease_expires_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS extracted_jobs (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                normalized_url TEXT NOT NULL UNIQUE,
                page_url TEXT NOT NULL,
                canonical_url TEXT,
                job_url TEXT,
                identifier TEXT,
                discovery_result_id BIGINT,
                source_name TEXT,
                source_url TEXT,
                status TEXT NOT NULL
                    CHECK(status IN ('incomplete', 'complete')),
                extraction_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discovery_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                municipality_code TEXT NOT NULL,
                municipality_name TEXT NOT NULL,
                state TEXT NOT NULL,
                category TEXT NOT NULL,
                query TEXT NOT NULL,
                normalized_url TEXT NOT NULL,
                result_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE (municipality_code, category, query, normalized_url)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discovery_result_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discovery_result_id INTEGER NOT NULL,
                previous_result_json TEXT NOT NULL,
                previous_last_seen_at TEXT NOT NULL,
                archived_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS extraction_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discovery_result_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'running', 'completed', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts >= 1),
                available_at TEXT NOT NULL,
                lease_token TEXT,
                lease_expires_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS extracted_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_url TEXT NOT NULL UNIQUE,
                page_url TEXT NOT NULL,
                canonical_url TEXT,
                job_url TEXT,
                identifier TEXT,
                discovery_result_id INTEGER,
                source_name TEXT,
                source_url TEXT,
                status TEXT NOT NULL CHECK(status IN ('incomplete', 'complete')),
                extraction_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

    indexes = (
        "CREATE INDEX IF NOT EXISTS ix_discovery_results_municipality "
        "ON discovery_results(municipality_code)",
        "CREATE INDEX IF NOT EXISTS ix_discovery_results_state "
        "ON discovery_results(state)",
        "CREATE INDEX IF NOT EXISTS ix_discovery_history_result "
        "ON discovery_result_history(discovery_result_id, id)",
        "CREATE INDEX IF NOT EXISTS ix_extraction_queue_pending "
        "ON extraction_queue(status, available_at, id)",
        "CREATE INDEX IF NOT EXISTS ix_extraction_queue_lease "
        "ON extraction_queue(status, lease_expires_at)",
        "CREATE INDEX IF NOT EXISTS ix_extracted_jobs_status "
        "ON extracted_jobs(status, last_seen_at)",
    )
    for statement in indexes:
        conn.execute(statement)


def init_db():
    """Cria e atualiza tabelas sem apagar registros; migrações são serializadas."""
    with db_session(immediate=True) as conn:
        if DATABASE_URL:
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,)
            )

        _create_candidate_and_source_tables(conn)
        _ensure_column(conn, "candidates", "normalized_url", "TEXT")
        _ensure_column(
            conn, "candidates", "evidence_json", "TEXT NOT NULL DEFAULT '{}'"
        )
        _ensure_column(conn, "sources", "official_site_url", "TEXT")
        _ensure_column(
            conn, "sources", "authorization_evidence_url", "TEXT"
        )

        rows = execute(
            conn,
            """
            SELECT id, original_job_url
            FROM candidates
            WHERE normalized_url IS NULL OR normalized_url = ''
            ORDER BY id
            """,
        ).fetchall()
        for row in rows:
            try:
                normalized = normalize_job_url(row["original_job_url"])
            except ValueError as exc:
                raise RuntimeError(
                    "Migração interrompida: URL original inválida "
                    f"na oportunidade ID {row['id']}. Nenhum registro foi apagado."
                ) from exc
            execute(
                conn,
                "UPDATE candidates SET normalized_url = ? WHERE id = ?",
                (normalized, row["id"]),
            )

        duplicates = execute(
            conn,
            """
            SELECT normalized_url, COUNT(*) AS total
            FROM candidates
            WHERE normalized_url IS NOT NULL AND normalized_url != ''
            GROUP BY normalized_url
            HAVING COUNT(*) > 1
            LIMIT 5
            """,
        ).fetchall()
        if duplicates:
            examples = ", ".join(
                f"{row['normalized_url']} ({row['total']} registros)"
                for row in duplicates
            )
            raise RuntimeError(
                "Migração interrompida: existem URLs normalizadas duplicadas "
                "em candidates. Nenhum registro foi apagado. "
                f"Exemplos: {examples}"
            )

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_candidates_normalized_url
            ON candidates(normalized_url)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_sources_enabled ON sources(enabled)
        """)
        _create_discovery_tables(conn)


def _validate_municipality_fields(
    municipality_code: str,
    municipality_name: str,
    state: str,
    category: str,
    query: str,
):
    municipality_code = str(municipality_code).strip()
    municipality_name = str(municipality_name).strip()
    state = str(state).strip().upper()
    category = str(category).strip()
    query = str(query).strip()
    if not re.fullmatch(r"\d{7}", municipality_code):
        raise ValueError("Código IBGE do município inválido.")
    if not municipality_name:
        raise ValueError("Nome do município obrigatório.")
    if not re.fullmatch(r"[A-Z]{2}", state):
        raise ValueError("UF inválida.")
    if not category:
        raise ValueError("Categoria da descoberta obrigatória.")
    if not query:
        raise ValueError("Consulta de pesquisa obrigatória.")
    return municipality_code, municipality_name, state, category, query


def save_discovery_result(
    *,
    municipality_code: str,
    municipality_name: str,
    state: str,
    category: str,
    query: str,
    normalized_url: str,
    result,
):
    """Armazena descoberta bruta e arquiva a versão anterior quando ela muda."""
    (
        municipality_code,
        municipality_name,
        state,
        category,
        query,
    ) = _validate_municipality_fields(
        municipality_code, municipality_name, state, category, query
    )
    normalized_url = normalize_job_url(normalized_url)
    result_json = _serialize_discovery_payload(result)
    now = utc_now()

    with db_session(immediate=True) as conn:
        existing = execute(
            conn,
            """
            SELECT id, result_json, last_seen_at
            FROM discovery_results
            WHERE municipality_code = ? AND category = ?
              AND query = ? AND normalized_url = ?
            """ + (" FOR UPDATE" if DATABASE_URL else ""),
            (municipality_code, category, query, normalized_url),
        ).fetchone()

        if existing and existing["result_json"] != result_json:
            execute(
                conn,
                """
                INSERT INTO discovery_result_history (
                    discovery_result_id, previous_result_json,
                    previous_last_seen_at, archived_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    existing["id"],
                    existing["result_json"],
                    existing["last_seen_at"],
                    now,
                ),
            )

        cur = execute(
            conn,
            """
            INSERT INTO discovery_results (
                municipality_code, municipality_name, state, category,
                query, normalized_url, result_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (municipality_code, category, query, normalized_url)
            DO UPDATE SET
                municipality_name = excluded.municipality_name,
                state = excluded.state,
                result_json = excluded.result_json,
                last_seen_at = excluded.last_seen_at
            RETURNING id
            """,
            (
                municipality_code, municipality_name, state, category,
                query, normalized_url, result_json, now, now,
            ),
        )
        row = cur.fetchone()

    return {
        "id": row["id"],
        "municipality_code": municipality_code,
        "municipality_name": municipality_name,
        "state": state,
        "category": category,
        "query": query,
        "normalized_url": normalized_url,
    }


def list_discovery_results(
    municipality_code: str | None = None,
    state: str | None = None,
    limit: int = 100,
):
    """Lista descobertas brutas; não as transforma em candidatas."""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("limit deve ser um número inteiro.") from exc
    conditions = []
    params = []
    if municipality_code is not None:
        code = str(municipality_code).strip()
        if not re.fullmatch(r"\d{7}", code):
            raise ValueError("Código IBGE do município inválido.")
        conditions.append("municipality_code = ?")
        params.append(code)
    if state is not None:
        normalized_state = str(state).strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", normalized_state):
            raise ValueError("UF inválida.")
        conditions.append("state = ?")
        params.append(normalized_state)

    sql = "SELECT * FROM discovery_results"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY last_seen_at DESC, id DESC LIMIT ?"
    params.append(limit)

    with db_session() as conn:
        rows = execute(conn, sql, tuple(params)).fetchall()

    results = []
    for row in rows:
        item = dict(row)
        item["result"] = _json_load(
            item.pop("result_json"),
            context=f"descoberta ID {row['id']}",
        )
        results.append(item)
    return results


def list_discovery_history(discovery_result_id: int, limit: int = 100):
    """Consulta versões anteriores arquivadas de uma descoberta."""
    _positive_integer(discovery_result_id, "discovery_result_id")
    limit = _positive_integer(limit, "limit", maximum=1000)
    with db_session() as conn:
        rows = execute(
            conn,
            """
            SELECT * FROM discovery_result_history
            WHERE discovery_result_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (discovery_result_id, limit),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["previous_result"] = _json_load(
            item.pop("previous_result_json"),
            context=f"histórico da descoberta ID {discovery_result_id}",
        )
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# Fila persistente de extração
# ---------------------------------------------------------------------------


def enqueue_extraction(
    discovery_result_id: int,
    *,
    max_attempts: int = 5,
):
    """Enfileira uma descoberta existente; chamadas repetidas são idempotentes."""
    _positive_integer(discovery_result_id, "discovery_result_id")
    _positive_integer(max_attempts, "max_attempts", maximum=100)
    now = utc_now()

    with db_session(immediate=True) as conn:
        exists = execute(
            conn,
            "SELECT id FROM discovery_results WHERE id = ?",
            (discovery_result_id,),
        ).fetchone()
        if not exists:
            raise ValueError("Resultado de descoberta não encontrado.")
        execute(
            conn,
            """
            INSERT INTO extraction_queue (
                discovery_result_id, status, attempts, max_attempts,
                available_at, created_at, updated_at
            ) VALUES (?, 'pending', 0, ?, ?, ?, ?)
            ON CONFLICT(discovery_result_id) DO NOTHING
            """,
            (discovery_result_id, max_attempts, now, now, now),
        )
        row = execute(
            conn,
            "SELECT * FROM extraction_queue WHERE discovery_result_id = ?",
            (discovery_result_id,),
        ).fetchone()
    return dict(row)


def enqueue_pending_extractions(
    limit: int = 500,
    *,
    max_attempts: int = 5,
):
    """Enfileira descobertas ainda não presentes na fila, em lote limitado."""
    limit = _positive_integer(limit, "limit", maximum=5000)
    _positive_integer(max_attempts, "max_attempts", maximum=100)
    now = utc_now()
    with db_session(immediate=True) as conn:
        lock_clause = " FOR UPDATE OF d SKIP LOCKED" if DATABASE_URL else ""
        rows = execute(
            conn,
            """
            SELECT d.id
            FROM discovery_results d
            LEFT JOIN extraction_queue q ON q.discovery_result_id = d.id
            WHERE q.id IS NULL
            ORDER BY d.id ASC
            LIMIT ?
            """ + lock_clause,
            (limit,),
        ).fetchall()
        inserted_count = 0
        for row in rows:
            inserted = execute(
                conn,
                """
                INSERT INTO extraction_queue (
                    discovery_result_id, status, attempts, max_attempts,
                    available_at, created_at, updated_at
                ) VALUES (?, 'pending', 0, ?, ?, ?, ?)
                ON CONFLICT(discovery_result_id) DO NOTHING
                RETURNING id
                """,
                (row["id"], max_attempts, now, now, now),
            ).fetchone()
            if inserted:
                inserted_count += 1
    return inserted_count


def claim_next_extraction(*, lease_seconds: int = 300):
    """Reserva uma tarefa com lease; concorrentes não recebem a mesma tarefa."""
    lease_seconds = _positive_integer(
        lease_seconds, "lease_seconds", maximum=86_400
    )
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
    token = secrets.token_urlsafe(32)

    with db_session(
        immediate=True,
        statement_timeout_ms=10_000,
        lock_timeout_ms=5_000,
    ) as conn:
        lock_clause = " FOR UPDATE OF q SKIP LOCKED" if DATABASE_URL else ""
        row = execute(
            conn,
            """
            SELECT q.id, q.discovery_result_id, q.attempts, q.max_attempts
            FROM extraction_queue q
            WHERE q.status = 'pending' AND q.available_at <= ?
            ORDER BY q.available_at ASC, q.id ASC
            LIMIT 1
            """ + lock_clause,
            (now,),
        ).fetchone()
        if not row:
            return None

        updated = execute(
            conn,
            """
            UPDATE extraction_queue
            SET status = 'running', attempts = attempts + 1,
                lease_token = ?, lease_expires_at = ?,
                last_error = NULL, updated_at = ?
            WHERE id = ? AND status = 'pending'
            RETURNING id, discovery_result_id, attempts, max_attempts,
                      lease_token, lease_expires_at
            """,
            (token, expires_at, now, row["id"]),
        ).fetchone()
        if not updated:
            return None

        discovery = execute(
            conn,
            """
            SELECT id, municipality_code, municipality_name, state,
                   category, query, normalized_url, result_json,
                   first_seen_at, last_seen_at
            FROM discovery_results WHERE id = ?
            """,
            (updated["discovery_result_id"],),
        ).fetchone()
        if not discovery:
            # A descoberta não deve desaparecer, mas não deixamos lease órfão.
            execute(
                conn,
                """
                UPDATE extraction_queue
                SET status = 'failed', lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                ("Resultado de descoberta não encontrado.", now, updated["id"]),
            )
            return None

        discovery_data = dict(discovery)
        discovery_data["result"] = _json_load(
            discovery_data.pop("result_json"),
            context=f"descoberta ID {discovery['id']}",
        )
        return {
            **dict(updated),
            "discovery_result": discovery_data,
        }


def heartbeat_extraction(
    queue_id: int,
    lease_token: str,
    *,
    lease_seconds: int = 300,
):
    """Renova o lease somente para o worker que ainda possui o token atual."""
    _positive_integer(queue_id, "queue_id")
    _positive_integer(lease_seconds, "lease_seconds", maximum=86_400)
    if not isinstance(lease_token, str) or not lease_token:
        raise ValueError("lease_token obrigatório.")
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
    with db_session(immediate=True) as conn:
        row = execute(
            conn,
            """
            UPDATE extraction_queue
            SET lease_expires_at = ?, updated_at = ?
            WHERE id = ? AND status = 'running' AND lease_token = ?
            RETURNING id, discovery_result_id, attempts, max_attempts,
                      lease_expires_at
            """,
            (expires, now, queue_id, lease_token),
        ).fetchone()
    return dict(row) if row else None


def mark_extraction_completed(queue_id: int, lease_token: str):
    """Conclui tarefa somente se o token de lease ainda for o atual."""
    _positive_integer(queue_id, "queue_id")
    if not isinstance(lease_token, str) or not lease_token:
        raise ValueError("lease_token obrigatório.")
    now = utc_now()
    with db_session(immediate=True) as conn:
        row = execute(
            conn,
            """
            UPDATE extraction_queue
            SET status = 'completed', lease_token = NULL,
                lease_expires_at = NULL, last_error = NULL,
                completed_at = ?, updated_at = ?
            WHERE id = ? AND status = 'running' AND lease_token = ?
            RETURNING id, discovery_result_id, status, attempts, completed_at
            """,
            (now, now, queue_id, lease_token),
        ).fetchone()
    return dict(row) if row else None


def mark_extraction_failed(
    queue_id: int,
    lease_token: str,
    error: str,
    *,
    retry_delay_seconds: int = 300,
):
    """Registra falha; devolve a tarefa à fila até atingir max_attempts."""
    _positive_integer(queue_id, "queue_id")
    _positive_integer(
        retry_delay_seconds, "retry_delay_seconds", maximum=7 * 24 * 3600
    )
    if not isinstance(lease_token, str) or not lease_token:
        raise ValueError("lease_token obrigatório.")
    if not isinstance(error, str) or not error.strip():
        raise ValueError("Mensagem de erro obrigatória.")
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    available = (now_dt + timedelta(seconds=retry_delay_seconds)).isoformat()
    safe_error = error.strip()[:2000]

    with db_session(immediate=True) as conn:
        current = execute(
            conn,
            """
            SELECT attempts, max_attempts FROM extraction_queue
            WHERE id = ? AND status = 'running' AND lease_token = ?
            """,
            (queue_id, lease_token),
        ).fetchone()
        if not current:
            return None
        next_status = (
            "failed" if current["attempts"] >= current["max_attempts"]
            else "pending"
        )
        row = execute(
            conn,
            """
            UPDATE extraction_queue
            SET status = ?, available_at = ?, lease_token = NULL,
                lease_expires_at = NULL, last_error = ?, updated_at = ?
            WHERE id = ? AND status = 'running' AND lease_token = ?
            RETURNING id, discovery_result_id, status, attempts,
                      max_attempts, available_at, last_error
            """,
            (next_status, available, safe_error, now, queue_id, lease_token),
        ).fetchone()
    return dict(row) if row else None


def recover_interrupted_extractions(*, stale_after_seconds: int = 900):
    """Recupera leases vencidos sem permitir que um worker antigo os conclua."""
    if (
        isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, int)
        or stale_after_seconds < 0
        or stale_after_seconds > 7 * 24 * 3600
    ):
        raise ValueError(
            "stale_after_seconds deve ser um inteiro não negativo de até sete dias."
        )
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    ).isoformat()
    now = utc_now()
    with db_session(immediate=True) as conn:
        rows = execute(
            conn,
            """
            UPDATE extraction_queue
            SET status = CASE
                    WHEN attempts >= max_attempts THEN 'failed'
                    ELSE 'pending'
                END,
                available_at = ?, lease_token = NULL,
                lease_expires_at = NULL,
                last_error = CASE
                    WHEN last_error IS NULL OR last_error = ''
                    THEN 'Lease expirado; tarefa recuperada.'
                    ELSE substr(last_error || ' | Lease expirado; tarefa recuperada.', 1, 2000)
                END,
                updated_at = ?
            WHERE status = 'running'
              AND (
                    (lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                    OR (lease_expires_at IS NULL AND updated_at <= ?)
              )
            RETURNING id
            """,
            (now, now, now, cutoff),
        ).fetchall()
    return len(rows)


def list_extraction_queue(
    status: str | None = None,
    limit: int = 100,
):
    """Lista tarefas da fila sem expor tokens de lease."""
    limit = _positive_integer(limit, "limit", maximum=1000)
    if status is not None and status not in EXTRACTION_STATUSES:
        raise ValueError("Status de extração inválido.")
    with db_session() as conn:
        if status:
            rows = execute(
                conn,
                """
                SELECT id, discovery_result_id, status, attempts, max_attempts,
                       available_at, lease_expires_at, last_error,
                       created_at, updated_at, completed_at
                FROM extraction_queue WHERE status = ?
                ORDER BY id DESC LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        else:
            rows = execute(
                conn,
                """
                SELECT id, discovery_result_id, status, attempts, max_attempts,
                       available_at, lease_expires_at, last_error,
                       created_at, updated_at, completed_at
                FROM extraction_queue ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Armazenamento intermediário de extrações
# ---------------------------------------------------------------------------


def _coerce_extracted_job(value) -> ExtractedJobData:
    if isinstance(value, ExtractedJobData):
        return value
    if hasattr(value, "model_dump"):
        data = value.model_dump(mode="json")
    elif is_dataclass(value) and not isinstance(value, type):
        data = asdict(value)
    elif isinstance(value, dict):
        data = value
    elif hasattr(value, "__dict__"):
        data = vars(value)
    else:
        raise TypeError("Formato de extração não suportado.")
    return ExtractedJobData.model_validate(data)


def _merge_unique_strings(old_values, new_values, *, limit: int = 50):
    result = []
    seen = set()
    for value in [*(old_values or []), *(new_values or [])]:
        text = str(value).strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _merge_extracted_payload(old_data: dict, new_data: dict) -> dict:
    merged = dict(old_data)
    for key, new_value in new_data.items():
        old_value = merged.get(key)
        if key in {"description", "requirements"}:
            if isinstance(new_value, str) and new_value.strip():
                if not isinstance(old_value, str) or len(new_value.strip()) > len(old_value.strip()):
                    merged[key] = new_value
        elif key in {"application_urls", "missing_fields", "warnings"}:
            merged[key] = _merge_unique_strings(
                old_value,
                new_value,
                limit=20 if key == "application_urls" else 50,
            )
        elif key in {"title", "location", "employment_type", "company", "company_hint"}:
            if isinstance(new_value, str) and new_value.strip():
                old_is_placeholder = _is_placeholder(old_value)
                new_is_placeholder = _is_placeholder(new_value)
                if old_is_placeholder and not new_is_placeholder:
                    merged[key] = new_value
                elif key in {"title", "location"} and len(new_value.strip()) > len(str(old_value or "").strip()):
                    merged[key] = new_value
        elif key == "mode":
            if new_value and new_value != "Não informado":
                merged[key] = new_value
        elif isinstance(new_value, bool):
            merged[key] = bool(old_value) or new_value
        elif key == "valid_through" and new_value:
            if old_value in (None, "") or str(new_value) > str(old_value):
                merged[key] = new_value
        elif new_value not in (None, "", [], {}):
            if old_value in (None, "", [], {}):
                merged[key] = new_value
        # An extraction with missing values never erases a previous value.
    return merged


def save_extracted_job(
    extracted_job,
    *,
    discovery_result_id: int | None = None,
    source_name: str | None = None,
    source_url: str | None = None,
):
    """Persiste extração incompleta ou completa sem criar candidato ou pontuar."""
    job = _coerce_extracted_job(extracted_job)
    if discovery_result_id is not None:
        _positive_integer(discovery_result_id, "discovery_result_id")
    if source_name is not None and (
        not isinstance(source_name, str) or not source_name.strip()
    ):
        raise ValueError("source_name deve ser uma string não vazia.")
    if source_url is not None:
        source_url = normalize_job_url(source_url)

    payload = job.model_dump(mode="json")
    identity_url = (
        str(job.canonical_url or job.job_url or job.page_url)
    )
    normalized_url = normalize_job_url(identity_url)
    status = "complete" if job.title and job.company else "incomplete"
    now = utc_now()

    with db_session(immediate=True) as conn:
        existing = execute(
            conn,
            "SELECT id, extraction_json, first_seen_at FROM extracted_jobs WHERE normalized_url = ?"
            + (" FOR UPDATE" if DATABASE_URL else ""),
            (normalized_url,),
        ).fetchone()
        if existing:
            previous = _json_load(
                existing["extraction_json"],
                context=f"extração ID {existing['id']}",
            )
            payload = _merge_extracted_payload(previous, payload)
            job = ExtractedJobData.model_validate(payload)
            status = "complete" if job.title and job.company else "incomplete"
            cur = execute(
                conn,
                """
                UPDATE extracted_jobs
                SET page_url = ?, canonical_url = ?, job_url = ?, identifier = ?,
                    discovery_result_id = COALESCE(?, discovery_result_id),
                    source_name = COALESCE(?, source_name),
                    source_url = COALESCE(?, source_url), status = ?,
                    extraction_json = ?, last_seen_at = ?, updated_at = ?
                WHERE id = ?
                RETURNING id
                """,
                (
                    str(job.page_url),
                    str(job.canonical_url) if job.canonical_url else None,
                    str(job.job_url) if job.job_url else None,
                    job.identifier,
                    discovery_result_id,
                    source_name.strip() if source_name else None,
                    source_url,
                    status,
                    _json_dumps(job.model_dump(mode="json")),
                    now,
                    now,
                    existing["id"],
                ),
            )
            row = cur.fetchone()
            outcome = "updated"
        else:
            cur = execute(
                conn,
                """
                INSERT INTO extracted_jobs (
                    normalized_url, page_url, canonical_url, job_url,
                    identifier, discovery_result_id, source_name, source_url,
                    status, extraction_json, first_seen_at, last_seen_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(normalized_url) DO NOTHING
                RETURNING id
                """,
                (
                    normalized_url,
                    str(job.page_url),
                    str(job.canonical_url) if job.canonical_url else None,
                    str(job.job_url) if job.job_url else None,
                    job.identifier,
                    discovery_result_id,
                    source_name.strip() if source_name else None,
                    source_url,
                    status,
                    _json_dumps(payload),
                    now,
                    now,
                    now,
                    now,
                ),
            )
            row = cur.fetchone()
            outcome = "created"
            if not row:
                # Handles a simultaneous insert on PostgreSQL without losing data.
                existing = execute(
                    conn,
                    "SELECT id, extraction_json FROM extracted_jobs WHERE normalized_url = ?"
                    + (" FOR UPDATE" if DATABASE_URL else ""),
                    (normalized_url,),
                ).fetchone()
                if not existing:
                    raise RuntimeError("Não foi possível localizar a extração salva.")
                previous = _json_load(
                    existing["extraction_json"],
                    context=f"extração ID {existing['id']}",
                )
                merged = ExtractedJobData.model_validate(
                    _merge_extracted_payload(previous, payload)
                )
                status = "complete" if merged.title and merged.company else "incomplete"
                cur = execute(
                    conn,
                    """
                    UPDATE extracted_jobs
                    SET page_url = ?, canonical_url = ?, job_url = ?,
                        identifier = ?, extraction_json = ?, status = ?,
                        last_seen_at = ?, updated_at = ?,
                        discovery_result_id = COALESCE(?, discovery_result_id),
                        source_name = COALESCE(?, source_name),
                        source_url = COALESCE(?, source_url)
                    WHERE id = ? RETURNING id
                    """,
                    (
                        str(merged.page_url),
                        str(merged.canonical_url) if merged.canonical_url else None,
                        str(merged.job_url) if merged.job_url else None,
                        merged.identifier,
                        _json_dumps(merged.model_dump(mode="json")), status,
                        now, now, discovery_result_id,
                        source_name.strip() if source_name else None,
                        source_url, existing["id"],
                    ),
                )
                row = cur.fetchone()
                outcome = "updated"

    return {
        "id": row["id"],
        "normalized_url": normalized_url,
        "status": status,
        "result": outcome,
    }


def get_extracted_job(normalized_url: str):
    """Recupera uma extração pelo endereço canônico normalizado."""
    normalized_url = normalize_job_url(normalized_url)
    with db_session() as conn:
        row = execute(
            conn,
            "SELECT * FROM extracted_jobs WHERE normalized_url = ?",
            (normalized_url,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["extracted_job"] = ExtractedJobData.model_validate(
        _json_load(
            result.pop("extraction_json"),
            context=f"extração ID {row['id']}",
        )
    ).model_dump(mode="json")
    return result


def list_extracted_jobs(
    status: str | None = None,
    limit: int = 100,
):
    """Lista extrações intermediárias, sem atribuir aprovação ou pontuação."""
    limit = _positive_integer(limit, "limit", maximum=1000)
    if status is not None and status not in EXTRACTED_JOB_STATUSES:
        raise ValueError("Status de extração intermediária inválido.")
    with db_session() as conn:
        if status:
            rows = execute(
                conn,
                "SELECT * FROM extracted_jobs WHERE status = ? ORDER BY last_seen_at DESC, id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = execute(
                conn,
                "SELECT * FROM extracted_jobs ORDER BY last_seen_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["extracted_job"] = ExtractedJobData.model_validate(
            _json_load(
                item.pop("extraction_json"),
                context=f"extração ID {row['id']}",
            )
        ).model_dump(mode="json")
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# Evidências, candidatos e fontes
# ---------------------------------------------------------------------------


def _evidence_timestamp(record: EvidenceRecord) -> float:
    return CandidateEvidence._timestamp(record.checked_at)


def _merge_evidence(
    existing_data: dict | None,
    new_evidence: CandidateEvidence,
) -> CandidateEvidence:
    existing = CandidateEvidence.model_validate(existing_data or {})
    records_by_key = {}
    for record in [*existing.evidence_records, *new_evidence.evidence_records]:
        encoded = _json_dumps(record.model_dump(mode="json"))
        records_by_key[encoded] = record
    records = list(records_by_key.values())
    records.sort(key=_evidence_timestamp, reverse=True)
    records = records[:250]

    flags = {}
    for criterion, flag_name in (
        ("company_verified", "company_verified"),
        ("original_page_verified", "original_page_verified"),
        ("application_link_verified", "application_link_verified"),
        ("job_content_verified", "job_content_verified"),
        ("date_verified", "date_verified"),
    ):
        matching = [record for record in records if record.criterion == criterion]
        if not matching:
            flags[flag_name] = False
            continue
        latest_timestamp = max(_evidence_timestamp(record) for record in matching)
        latest = [
            record for record in matching
            if _evidence_timestamp(record) == latest_timestamp
        ]
        flags[flag_name] = all(record.result == "verified" for record in latest)

    return CandidateEvidence(
        **flags,
        evidence_records=records,
        rejected_reason=(
            new_evidence.rejected_reason or existing.rejected_reason
        ),
    )


def _is_placeholder(value) -> bool:
    if not isinstance(value, str):
        return True
    normalized = re.sub(r"\s+", " ", value.strip().casefold())
    return (
        not normalized
        or normalized in {
            "não informado", "nao informado", "não identificada",
            "nao identificada", "desconhecida", "unknown", "n/a",
            "confidencial", "empresa confidencial",
        }
        or "confidencial" in normalized
    )


def _merge_candidate_payload(existing_payload: dict, new_payload: dict) -> CandidateIn:
    """Mescla somente dados úteis; campos ausentes nunca apagam conteúdo."""
    allowed = set(CandidateIn.model_fields)
    merged = {key: value for key, value in existing_payload.items() if key in allowed}

    long_text_fields = {"description", "requirements"}
    append_fields = {"application_urls", "missing_fields", "extraction_warnings"}
    url_fields = {"application_url"}

    for key, new_value in new_payload.items():
        if key not in allowed:
            continue
        old_value = merged.get(key)

        if key == "company":
            if _is_placeholder(old_value) and not _is_placeholder(new_value):
                merged[key] = new_value
        elif key in long_text_fields:
            if isinstance(new_value, str) and new_value.strip():
                if not isinstance(old_value, str) or len(new_value) > len(old_value):
                    merged[key] = new_value
        elif key in append_fields:
            merged[key] = _merge_unique_strings(
                old_value, new_value, limit=50 if key != "application_urls" else 20
            )
        elif key in url_fields:
            if new_value:
                merged[key] = new_value
        elif key == "mode":
            if new_value and new_value != "Não informado":
                merged[key] = new_value
        elif key in {"title", "location", "employment_type", "advertiser_company", "hiring_company", "intermediary_name", "company_hint", "date_posted", "valid_through", "canonical_url", "identifier", "job_url", "extraction_method"}:
            if new_value not in (None, ""):
                if old_value in (None, ""):
                    merged[key] = new_value
                elif key in {"title", "location"} and len(str(new_value)) > len(str(old_value)):
                    merged[key] = new_value
                elif key in {"employment_type", "advertiser_company", "hiring_company", "intermediary_name", "company_hint"} and _is_placeholder(old_value) and not _is_placeholder(new_value):
                    merged[key] = new_value
                elif key == "valid_through":
                    # A data de validade posterior é preferida se ambas forem válidas.
                    if str(new_value) > str(old_value):
                        merged[key] = new_value
        elif key in {"structured_data_found"}:
            merged[key] = bool(old_value) or bool(new_value)
        elif key in {"source_name", "source_url", "original_job_url"}:
            if old_value in (None, "") and new_value not in (None, ""):
                merged[key] = new_value
        elif new_value not in (None, "", [], {}):
            if old_value in (None, "", [], {}):
                merged[key] = new_value

    # Reconstroi o modelo para sincronizar application_url/application_urls e
    # garantir que a mesclagem ainda respeite todas as validações de CandidateIn.
    return CandidateIn.model_validate(merged)


def save_candidate(
    candidate: CandidateIn,
    evidence: CandidateEvidence | None = None,
):
    """Insere uma candidata ou enriquece registro existente pela URL normalizada."""
    if not isinstance(candidate, CandidateIn):
        candidate = CandidateIn.model_validate(candidate)
    evidence = evidence or CandidateEvidence()
    payload = candidate.model_dump(mode="json")
    incoming_evidence_json = evidence.model_dump(mode="json")
    now = utc_now()
    original_url = str(candidate.original_job_url)
    normalized_url = normalize_job_url(original_url)

    with db_session(
        immediate=True,
        statement_timeout_ms=15_000,
        lock_timeout_ms=5_000,
    ) as conn:
        inserted = execute(
            conn,
            """
            INSERT INTO candidates (
                title, company, location, mode, source_name, source_url,
                original_job_url, normalized_url, application_url, score,
                status, reasons_json, payload_json, evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_url) DO NOTHING
            RETURNING id
            """,
            (
                candidate.title,
                candidate.company,
                candidate.location,
                candidate.mode,
                candidate.source_name,
                str(candidate.source_url),
                original_url,
                normalized_url,
                str(candidate.application_url) if candidate.application_url else None,
                0,
                "review",
                "[]",
                _json_dumps(payload),
                _json_dumps(incoming_evidence_json),
                now,
            ),
        ).fetchone()

        if inserted:
            score, status, reasons = score_candidate(evidence)
            execute(
                conn,
                """
                UPDATE candidates
                SET score = ?, status = ?, reasons_json = ?
                WHERE id = ?
                """,
                (score, status, _json_dumps(reasons), inserted["id"]),
            )
            return {
                "id": inserted["id"],
                "status": status,
                "score": score,
                "reasons": reasons,
            }

        lock_clause = " FOR UPDATE" if DATABASE_URL else ""
        existing = execute(
            conn,
            """
            SELECT * FROM candidates WHERE normalized_url = ?
            """ + lock_clause,
            (normalized_url,),
        ).fetchone()
        if not existing:
            raise RuntimeError(
                "Não foi possível salvar ou localizar a oportunidade."
            )

        old_payload = _json_load(
            existing["payload_json"],
            context=f"candidata ID {existing['id']}",
        )
        old_evidence = _json_load(
            existing["evidence_json"],
            context=f"evidências da candidata ID {existing['id']}",
        )
        merged_candidate = _merge_candidate_payload(old_payload, payload)
        merged_evidence = _merge_evidence(old_evidence, evidence)
        score, status, reasons = score_candidate(merged_evidence)
        merged_payload = merged_candidate.model_dump(mode="json")

        # Colunas de consulta e JSON são atualizadas na mesma transação.
        execute(
            conn,
            """
            UPDATE candidates
            SET title = ?, company = ?, location = ?, mode = ?,
                source_name = ?, source_url = ?, application_url = ?,
                score = ?, status = ?, reasons_json = ?, payload_json = ?,
                evidence_json = ?
            WHERE id = ?
            """,
            (
                merged_candidate.title,
                merged_candidate.company,
                merged_candidate.location,
                merged_candidate.mode,
                merged_candidate.source_name,
                str(merged_candidate.source_url),
                str(merged_candidate.application_url)
                if merged_candidate.application_url else None,
                score,
                status,
                _json_dumps(reasons),
                _json_dumps(merged_payload),
                _json_dumps(merged_evidence.model_dump(mode="json")),
                existing["id"],
            ),
        )

        return {
            "id": existing["id"],
            "status": "duplicate",
            "existing_status": status,
            "score": score,
            "reasons": reasons,
        }


def save_source(source: SourceCreate):
    """Armazena fonte e dados declarados de legitimidade; não a ativa por padrão."""
    if not isinstance(source, SourceCreate):
        source = SourceCreate.model_validate(source)
    data = source.model_dump(mode="json")
    now = utc_now()
    with db_session(immediate=True) as conn:
        inserted = execute(
            conn,
            """
            INSERT INTO sources (
                source_id, name, source_type, base_url, official_site_url,
                authorization_evidence_url, "authorization", enabled,
                notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO NOTHING
            RETURNING source_id
            """,
            (
                data["source_id"], data["name"], data["source_type"],
                str(data["base_url"]),
                str(data["official_site_url"]) if data.get("official_site_url") else None,
                str(data["authorization_evidence_url"])
                if data.get("authorization_evidence_url") else None,
                data["authorization"], data["enabled"], data.get("notes"),
                now, now,
            ),
        ).fetchone()
        if not inserted:
            raise ValueError("Já existe uma fonte com esse identificador.")
    return get_source_record(data["source_id"])


def get_source_record(source_id: str):
    with db_session() as conn:
        row = execute(
            conn,
            "SELECT * FROM sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
    return serialize_source(row) if row else None


def list_sources(enabled_only: bool = False, limit: int = 500):
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("limit deve ser um número inteiro.") from exc
    with db_session() as conn:
        if enabled_only:
            rows = execute(
                conn,
                "SELECT * FROM sources WHERE enabled = ? ORDER BY name ASC LIMIT ?",
                (True, limit),
            ).fetchall()
        else:
            rows = execute(
                conn,
                "SELECT * FROM sources ORDER BY name ASC LIMIT ?",
                (limit,),
            ).fetchall()
    return [serialize_source(row) for row in rows]


def update_source_status(source_id: str, enabled: bool):
    if not isinstance(enabled, bool):
        raise ValueError("enabled deve ser booleano.")
    with db_session(immediate=True) as conn:
        updated = execute(
            conn,
            """
            UPDATE sources SET enabled = ?, updated_at = ?
            WHERE source_id = ? RETURNING source_id
            """,
            (enabled, utc_now(), source_id),
        ).fetchone()
    return get_source_record(source_id) if updated else None


def list_candidates(status: str | None = None, limit: int = 50):
    """Lista candidatas e seus dados completos de extração e evidência."""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("limit deve ser um número inteiro.") from exc
    if status is not None and status not in CANDIDATE_STATUSES:
        raise ValueError("Status de candidata inválido.")
    with db_session() as conn:
        if status:
            rows = execute(
                conn,
                "SELECT * FROM candidates WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = execute(
                conn,
                "SELECT * FROM candidates ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    results = []
    for row in rows:
        payload = _json_load(
            row["payload_json"], context=f"candidata ID {row['id']}"
        )
        evidence_data = _json_load(
            row["evidence_json"], context=f"evidências da candidata ID {row['id']}"
        )
        try:
            evidence = CandidateEvidence.model_validate(evidence_data).model_dump(mode="json")
        except Exception as exc:
            raise RuntimeError(
                "Evidências inválidas ou corrompidas "
                f"na oportunidade ID {row['id']}."
            ) from exc

        results.append({
            "id": row["id"],
            "title": row["title"],
            "company": row["company"],
            "location": row["location"],
            "mode": row["mode"],
            "source_name": row["source_name"],
            "source_url": row["source_url"],
            "original_job_url": row["original_job_url"],
            "application_url": row["application_url"],
            "application_urls": payload.get("application_urls", []),
            "employment_type": payload.get("employment_type"),
            "advertiser_company": payload.get("advertiser_company"),
            "hiring_company": payload.get("hiring_company"),
            "intermediary_name": payload.get("intermediary_name"),
            "company_hint": payload.get("company_hint"),
            "description": payload.get("description"),
            "requirements": payload.get("requirements"),
            "date_posted": payload.get("date_posted"),
            "valid_through": payload.get("valid_through"),
            "canonical_url": payload.get("canonical_url"),
            "identifier": payload.get("identifier"),
            "job_url": payload.get("job_url"),
            "extraction_method": payload.get("extraction_method"),
            "structured_data_found": payload.get("structured_data_found", False),
            "missing_fields": payload.get("missing_fields", []),
            "extraction_warnings": payload.get("extraction_warnings", []),
            "score": row["score"],
            "status": row["status"],
            "reasons": _json_load(
                row["reasons_json"], context=f"motivos da candidata ID {row['id']}"
            ),
            "evidence": evidence,
            "created_at": row["created_at"],
        })
    return results
