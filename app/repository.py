
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.models import CandidateEvidence, CandidateIn, SourceCreate
from app.scoring import score_candidate


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = Path(os.getenv("DATABASE_PATH", "./data/staging.db"))

# Identificador fixo para serializar as migrações no PostgreSQL.
MIGRATION_LOCK_ID = 731202611

TRACKING_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "ref", "referrer"
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_job_url(value: str) -> str:
    """Valida e normaliza URLs, removendo parâmetros de rastreamento."""
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
            or not all(
                char.isalnum() or char == "-"
                for char in label
            )
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

    for key, val in parse_qsl(
        parts.query,
        keep_blank_values=True,
    ):
        lowered = key.lower()

        if lowered.startswith("utm_") or lowered in TRACKING_KEYS:
            continue

        query_items.append((key, val))

    query = urlencode(sorted(query_items))

    return urlunsplit((
        scheme,
        netloc,
        path,
        query,
        "",
    ))


def _convert_sql_placeholders(sql: str) -> str:
    """
    Converte placeholders ? para %s sem alterar conteúdo entre aspas
    ou comentários SQL.

    Também preserva delimitadores dollar-quoted do PostgreSQL.
    """
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

            if char == "?":
                result.append("%s")
            else:
                result.append(char)

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
    """Conecta ao Neon ou ao SQLite local."""
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
    """Valida um limite de tempo em milissegundos."""
    if value is None:
        return None

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise ValueError(
            f"{name} deve ser um inteiro positivo."
        )

    return value
    

@contextmanager
def db_session(
    immediate: bool = False,
    *,
    statement_timeout_ms: int | None = None,
    lock_timeout_ms: int | None = None,
):
    """
    Garante transação e fechamento da conexão.

    PostgreSQL:
    - statement_timeout_ms limita a duração de cada comando SQL.
    - lock_timeout_ms limita a espera por bloqueios.
    - Os limites são locais à transação.

    SQLite:
    - Preserva o timeout e busy_timeout existentes.
    - Mantém BEGIN IMMEDIATE quando solicitado.
    """
    statement_timeout_ms = _validate_timeout_ms(
        statement_timeout_ms,
        "statement_timeout_ms",
    )

    lock_timeout_ms = _validate_timeout_ms(
        lock_timeout_ms,
        "lock_timeout_ms",
    )

    conn = connect()

    try:
        if not DATABASE_URL and immediate:
            conn.execute("BEGIN IMMEDIATE")

        with conn:
            if DATABASE_URL:
                if statement_timeout_ms is not None:
                    conn.execute(
                        """
                        SELECT set_config(
                            'statement_timeout',
                            %s,
                            true
                        )
                        """,
                        (f"{statement_timeout_ms}ms",),
                    )

                if lock_timeout_ms is not None:
                    conn.execute(
                        """
                        SELECT set_config(
                            'lock_timeout',
                            %s,
                            true
                        )
                        """,
                        (f"{lock_timeout_ms}ms",),
                    )

            yield conn

    finally:
        conn.close()


def execute(conn, sql, params=()):
    """Compatibiliza parâmetros SQLite e PostgreSQL."""
    if DATABASE_URL:
        sql = _convert_sql_placeholders(sql)

    return conn.execute(sql, params)


def _get_table_columns(conn, table_name: str) -> set[str]:
    """Obtém as colunas existentes em SQLite ou PostgreSQL."""
    if table_name not in {"candidates", "sources"}:
        raise ValueError("Tabela não permitida para inspeção.")

    if DATABASE_URL:
        rows = execute(
            conn,
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = ?
            """,
            (table_name,),
        ).fetchall()

        return {row["column_name"] for row in rows}

    rows = conn.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return {row["name"] for row in rows}


def _ensure_column(
    conn,
    table_name: str,
    column_name: str,
    column_definition: str,
):
    """Adiciona uma coluna somente quando ela ainda não existe."""
    allowed_columns = {
        "candidates": {
            "normalized_url",
            "evidence_json",
        },
        "sources": {
            "official_site_url",
            "authorization_evidence_url",
        },
    }

    if column_name not in allowed_columns.get(table_name, set()):
        raise ValueError("Coluna não permitida para migração.")

    columns = _get_table_columns(conn, table_name)

    if column_name not in columns:
        if DATABASE_URL:
            conn.execute(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN IF NOT EXISTS "
                f"{column_name} {column_definition}"
            )
        else:
            conn.execute(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN {column_name} {column_definition}"
            )


def serialize_source(row):
    """Padroniza os dados da fonte entre SQLite e PostgreSQL."""
    if row is None:
        return None

    result = dict(row)
    result["enabled"] = bool(result["enabled"])

    return result


def _serialize_discovery_payload(result) -> str:
    """Converte o resultado bruto de pesquisa em JSON seguro."""
    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json")
    elif hasattr(result, "__dataclass_fields__"):
        from dataclasses import asdict
        payload = asdict(result)
    elif isinstance(result, dict):
        payload = result
    elif hasattr(result, "__dict__"):
        payload = vars(result)
    else:
        raise TypeError(
            "Formato de resultado de descoberta não suportado."
        )

    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Não foi possível serializar o resultado bruto."
        ) from exc


def init_db():
    """
    Cria e atualiza as tabelas sem apagar registros existentes.

    Serializa as migrações:
    - SQLite: BEGIN IMMEDIATE.
    - PostgreSQL: advisory transaction lock.
    """
    with db_session(immediate=True) as conn:
        if DATABASE_URL:
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (MIGRATION_LOCK_ID,),
            )

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

        # Migração compatível com bancos antigos.
        _ensure_column(
            conn,
            "candidates",
            "normalized_url",
            "TEXT",
        )

        _ensure_column(
            conn,
            "candidates",
            "evidence_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )

        _ensure_column(
            conn,
            "sources",
            "official_site_url",
            "TEXT",
        )

        _ensure_column(
            conn,
            "sources",
            "authorization_evidence_url",
            "TEXT",
        )

        rows = execute(
            conn,
            """
            SELECT id, original_job_url
            FROM candidates
            WHERE normalized_url IS NULL OR normalized_url = ''
            ORDER BY id
            """
        ).fetchall()

        for row in rows:
            try:
                normalized = normalize_job_url(
                    row["original_job_url"]
                )
            except ValueError as exc:
                raise RuntimeError(
                    "Migração interrompida: URL original inválida "
                    f"na oportunidade ID {row['id']}. "
                    "Nenhum registro foi apagado."
                ) from exc

            execute(
                conn,
                """
                UPDATE candidates
                SET normalized_url = ?
                WHERE id = ?
                """,
                (normalized, row["id"]),
            )

        duplicates = execute(
            conn,
            """
            SELECT normalized_url, COUNT(*) AS total
            FROM candidates
            WHERE normalized_url IS NOT NULL
              AND normalized_url != ''
            GROUP BY normalized_url
            HAVING COUNT(*) > 1
            LIMIT 5
            """
        ).fetchall()

        if duplicates:
            examples = ", ".join(
                f"{row['normalized_url']} ({row['total']} registros)"
                for row in duplicates
            )
            raise RuntimeError(
                "Migração interrompida: existem URLs normalizadas "
                "duplicadas em candidates. Nenhum registro foi apagado. "
                f"Exemplos: {examples}"
            )

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            ux_candidates_normalized_url
            ON candidates(normalized_url)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            ix_sources_enabled
            ON sources(enabled)
        """)

        # Resultados brutos das pesquisas municipais.
        # Esta tabela é independente de candidates e não aplica score.
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
                    UNIQUE (
                        municipality_code,
                        category,
                        query,
                        normalized_url
                    )
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
                    UNIQUE (
                        municipality_code,
                        category,
                        query,
                        normalized_url
                    )
                )
            """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            ix_discovery_results_municipality
            ON discovery_results(municipality_code)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            ix_discovery_results_state
            ON discovery_results(state)
        """)


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
    """
    Armazena uma descoberta bruta de pesquisa municipal.

    Não cria candidato, não calcula pontuação e não aprova oportunidades.
    A mesma descoberta atualiza last_seen_at sem perder first_seen_at.
    """
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

    normalized_url = normalize_job_url(normalized_url)
    result_json = _serialize_discovery_payload(result)
    now = utc_now()

    with db_session(immediate=True) as conn:
        cur = execute(
            conn,
            """
            INSERT INTO discovery_results (
                municipality_code,
                municipality_name,
                state,
                category,
                query,
                normalized_url,
                result_json,
                first_seen_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                municipality_code,
                category,
                query,
                normalized_url
            )
            DO UPDATE SET
                municipality_name = excluded.municipality_name,
                state = excluded.state,
                result_json = excluded.result_json,
                last_seen_at = excluded.last_seen_at
            RETURNING id
            """,
            (
                municipality_code,
                municipality_name,
                state,
                category,
                query,
                normalized_url,
                result_json,
                now,
                now,
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
    """Lista resultados brutos de descoberta, sem transformá-los em candidatos."""
    limit = max(1, min(int(limit), 1000))

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

    sql = """
        SELECT *
        FROM discovery_results
    """

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    sql += " ORDER BY last_seen_at DESC, id DESC LIMIT ?"
    params.append(limit)

    with db_session() as conn:
        rows = execute(conn, sql, tuple(params)).fetchall()

    results = []

    for row in rows:
        item = dict(row)

        try:
            item["result"] = json.loads(item.pop("result_json"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Resultado bruto inválido ou corrompido "
                f"no registro ID {row['id']}."
            ) from exc

        results.append(item)

    return results


def save_candidate(
    candidate: CandidateIn,
    evidence: CandidateEvidence | None = None,
):
    """Salva oportunidade, pontuação e evidências individuais."""
    evidence = evidence or CandidateEvidence()

    score, status, reasons = score_candidate(evidence)

    payload = candidate.model_dump(mode="json")
    evidence_payload = evidence.model_dump(mode="json")

    created_at = utc_now()

    original_url = str(candidate.original_job_url)
    normalized_url = normalize_job_url(original_url)

    with db_session(immediate=True) as conn:
        cur = execute(
            conn,
            """
            INSERT INTO candidates (
                title, company, location, mode, source_name, source_url,
                original_job_url, normalized_url, application_url, score,
                status, reasons_json, payload_json, evidence_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                str(candidate.application_url)
                if candidate.application_url else None,
                score,
                status,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(payload, ensure_ascii=False),
                json.dumps(evidence_payload, ensure_ascii=False),
                created_at,
            ),
        )

        inserted = cur.fetchone()

        if inserted:
            return {
                "id": inserted["id"],
                "status": status,
                "score": score,
                "reasons": reasons,
            }

        existing = execute(
            conn,
            """
            SELECT id, status, score
            FROM candidates
            WHERE normalized_url = ?
            """,
            (normalized_url,),
        ).fetchone()

        if existing:
            return {
                "id": existing["id"],
                "status": "duplicate",
                "existing_status": existing["status"],
                "score": existing["score"],
                "reasons": [
                    "URL original já cadastrada após normalização; "
                    "nenhum novo registro foi criado."
                ],
            }

        raise RuntimeError(
            "Não foi possível salvar ou localizar a oportunidade."
        )


def save_source(source: SourceCreate):
    """Armazena uma fonte e suas informações de legitimidade declaradas."""
    data = source.model_dump(mode="json")
    now = utc_now()

    with db_session(immediate=True) as conn:
        cur = execute(
            conn,
            """
            INSERT INTO sources (
                source_id,
                name,
                source_type,
                base_url,
                official_site_url,
                authorization_evidence_url,
                "authorization",
                enabled,
                notes,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO NOTHING
            RETURNING source_id
            """,
            (
                data["source_id"],
                data["name"],
                data["source_type"],
                str(data["base_url"]),
                str(data["official_site_url"])
                if data.get("official_site_url") else None,
                str(data["authorization_evidence_url"])
                if data.get("authorization_evidence_url") else None,
                data["authorization"],
                data["enabled"],
                data.get("notes"),
                now,
                now,
            ),
        )

        inserted = cur.fetchone()

        if not inserted:
            raise ValueError(
                "Já existe uma fonte com esse identificador."
            )

    return get_source_record(data["source_id"])


def get_source_record(source_id: str):
    """Consulta uma fonte pelo identificador."""
    with db_session() as conn:
        row = execute(
            conn,
            """
            SELECT *
            FROM sources
            WHERE source_id = ?
            """,
            (source_id,),
        ).fetchone()

    if not row:
        return None

    return serialize_source(row)


def list_sources(enabled_only: bool = False, limit: int = 500):
    """Lista fontes cadastradas."""
    limit = max(1, min(int(limit), 500))

    with db_session() as conn:
        if enabled_only:
            rows = execute(
                conn,
                """
                SELECT *
                FROM sources
                WHERE enabled = ?
                ORDER BY name ASC
                LIMIT ?
                """,
                (True, limit),
            ).fetchall()
        else:
            rows = execute(
                conn,
                """
                SELECT *
                FROM sources
                ORDER BY name ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    return [serialize_source(row) for row in rows]


def update_source_status(source_id: str, enabled: bool):
    """Ativa ou desativa uma fonte cadastrada."""
    with db_session(immediate=True) as conn:
        cur = execute(
            conn,
            """
            UPDATE sources
            SET enabled = ?, updated_at = ?
            WHERE source_id = ?
            RETURNING source_id
            """,
            (enabled, utc_now(), source_id),
        )

        updated = cur.fetchone()

    if not updated:
        return None

    return get_source_record(source_id)


def list_candidates(status: str | None = None, limit: int = 50):
    """Lista oportunidades incluindo os dados e evidências armazenados."""
    limit = max(1, min(int(limit), 500))

    with db_session() as conn:
        if status:
            rows = execute(
                conn,
                """
                SELECT *
                FROM candidates
                WHERE status = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        else:
            rows = execute(
                conn,
                """
                SELECT *
                FROM candidates
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    results = []

    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")

        try:
            evidence_data = json.loads(
                row["evidence_json"] or "{}"
            )

            evidence = CandidateEvidence.model_validate(
                evidence_data
            ).model_dump(mode="json")

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
            "employment_type": payload.get("employment_type"),
            "advertiser_company": payload.get("advertiser_company"),
            "hiring_company": payload.get("hiring_company"),
            "intermediary_name": payload.get("intermediary_name"),
            "date_posted": payload.get("date_posted"),
            "valid_through": payload.get("valid_through"),
            "score": row["score"],
            "status": row["status"],
            "reasons": json.loads(row["reasons_json"]),
            "evidence": evidence,
            "created_at": row["created_at"],
        })

    return results
