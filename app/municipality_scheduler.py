
import secrets
from datetime import datetime, timedelta, timezone

from app.repository import DATABASE_URL, db_session, execute


MAX_MUNICIPALITY_ATTEMPTS = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_batch_index(batch_index: int) -> int:
    if (
        isinstance(batch_index, bool)
        or not isinstance(batch_index, int)
        or batch_index < 0
    ):
        raise ValueError("O índice do lote deve ser um inteiro não negativo.")

    return batch_index


def _validate_municipality_code(municipality_code: str) -> str:
    code = str(municipality_code).strip()

    if len(code) != 7 or not code.isdigit():
        raise ValueError("Código IBGE inválido.")

    return code


def _validate_lease_token(lease_token: str) -> str:
    if not isinstance(lease_token, str) or not lease_token.strip():
        raise ValueError("Token de execução inválido.")

    return lease_token.strip()


def _municipality_data(municipality):
    """Aceita objetos Municipality ou dicionários equivalentes."""
    if isinstance(municipality, dict):
        code = municipality.get("code")
        name = municipality.get("name")
        state = municipality.get("state")
    else:
        code = getattr(municipality, "code", None)
        name = getattr(municipality, "name", None)
        state = getattr(municipality, "state", None)

    code = str(code).strip() if code is not None else ""
    name = str(name).strip() if name is not None else ""
    state = str(state).strip().upper() if state is not None else ""

    if (
        not code
        or not name
        or len(state) != 2
        or not state.isascii()
        or not state.isalpha()
    ):
        raise ValueError("Dados de município inválidos.")

    if len(code) != 7 or not code.isdigit():
        raise ValueError(
            "O código IBGE deve conter exatamente sete dígitos."
        )

    return code, name, state


def _ensure_lease_token_column(conn):
    """
    Adiciona lease_token a bancos existentes sem perder dados.
    """
    if DATABASE_URL:
        execute(
            conn,
            """
            ALTER TABLE municipality_discovery
            ADD COLUMN IF NOT EXISTS lease_token TEXT
            """,
        )
        return

    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(municipality_discovery)"
        ).fetchall()
    }

    if "lease_token" not in columns:
        conn.execute(
            """
            ALTER TABLE municipality_discovery
            ADD COLUMN lease_token TEXT
            """
        )


def init_municipality_scheduler():
    """Cria as tabelas próprias do controle de lotes municipais."""
    with db_session() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS municipality_discovery (
                municipality_code TEXT PRIMARY KEY,
                municipality_name TEXT NOT NULL,
                state TEXT NOT NULL,
                batch_index INTEGER NOT NULL CHECK(batch_index >= 0),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN (
                        'pending', 'running', 'completed', 'failed'
                    )),
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lease_token TEXT
            )
        """)

        _ensure_lease_token_column(conn)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            ix_municipality_discovery_batch_status
            ON municipality_discovery(batch_index, status)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS municipality_scheduler_state (
                state_id INTEGER PRIMARY KEY CHECK(state_id = 1),
                current_batch INTEGER NOT NULL DEFAULT 0
                    CHECK(current_batch >= 0),
                updated_at TEXT NOT NULL
            )
        """)


def register_municipality_batch(batch_index: int, municipalities):
    """
    Registra os municípios de um lote sem duplicar códigos IBGE.

    Municípios já cadastrados não têm seu estado ou lote sobrescritos.
    O lote atual só avança quando novos municípios são registrados.
    """
    batch_index = _validate_batch_index(batch_index)
    records = [_municipality_data(item) for item in municipalities]

    if not records:
        raise ValueError("O lote não pode estar vazio.")

    if len(records) > 60:
        raise ValueError("Um lote não pode conter mais de 60 municípios.")

    codes = [record[0] for record in records]

    if len(codes) != len(set(codes)):
        raise ValueError("O lote contém códigos IBGE duplicados.")

    now = utc_now()
    inserted_count = 0

    with db_session(immediate=True) as conn:
        for code, name, state in records:
            cur = execute(
                conn,
                """
                INSERT INTO municipality_discovery (
                    municipality_code,
                    municipality_name,
                    state,
                    batch_index,
                    status,
                    attempts,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                ON CONFLICT(municipality_code) DO NOTHING
                """,
                (code, name, state, batch_index, now, now),
            )

            if cur.rowcount > 0:
                inserted_count += 1

        if inserted_count > 0:
            execute(
                conn,
                """
                INSERT INTO municipality_scheduler_state (
                    state_id, current_batch, updated_at
                )
                VALUES (1, ?, ?)
                ON CONFLICT(state_id) DO UPDATE SET
                    current_batch = CASE
                        WHEN excluded.current_batch >
                             municipality_scheduler_state.current_batch
                        THEN excluded.current_batch
                        ELSE municipality_scheduler_state.current_batch
                    END,
                    updated_at = excluded.updated_at
                """,
                (batch_index, now),
            )

    return get_batch_progress(batch_index)


def get_current_batch():
    """Retorna o lote atual ou None se ainda não houver estado salvo."""
    with db_session() as conn:
        row = execute(
            conn,
            """
            SELECT current_batch
            FROM municipality_scheduler_state
            WHERE state_id = 1
            """,
        ).fetchone()

    return row["current_batch"] if row else None


def get_batch_progress(batch_index: int):
    """Retorna o resumo de andamento de um lote."""
    batch_index = _validate_batch_index(batch_index)

    with db_session() as conn:
        row = execute(
            conn,
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END)
                    AS pending,
                SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END)
                    AS running,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                    AS completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                    AS failed
            FROM municipality_discovery
            WHERE batch_index = ?
            """,
            (batch_index,),
        ).fetchone()

    return {
        "batch_index": batch_index,
        "total": row["total"] or 0,
        "pending": row["pending"] or 0,
        "running": row["running"] or 0,
        "completed": row["completed"] or 0,
        "failed": row["failed"] or 0,
    }


def list_batch_municipalities(
    batch_index: int,
    status: str | None = None,
):
    """Lista municípios de um lote, opcionalmente filtrados por estado."""
    batch_index = _validate_batch_index(batch_index)

    allowed_statuses = {"pending", "running", "completed", "failed"}

    if status is not None and status not in allowed_statuses:
        raise ValueError("Estado de município inválido.")

    with db_session() as conn:
        if status:
            rows = execute(
                conn,
                """
                SELECT *
                FROM municipality_discovery
                WHERE batch_index = ? AND status = ?
                ORDER BY state, municipality_name
                """,
                (batch_index, status),
            ).fetchall()
        else:
            rows = execute(
                conn,
                """
                SELECT *
                FROM municipality_discovery
                WHERE batch_index = ?
                ORDER BY state, municipality_name
                """,
                (batch_index,),
            ).fetchall()

    return [dict(row) for row in rows]


def claim_next_municipality(batch_index: int):
    """
    Reserva o próximo município pendente.

    PostgreSQL utiliza FOR UPDATE SKIP LOCKED.
    SQLite utiliza BEGIN IMMEDIATE.

    Cada reserva recebe um token exclusivo.
    """
    batch_index = _validate_batch_index(batch_index)
    is_postgres = bool(DATABASE_URL)

    with db_session(immediate=True) as conn:
        if is_postgres:
            row = execute(
                conn,
                """
                SELECT municipality_code
                FROM municipality_discovery
                WHERE batch_index = ?
                  AND status = 'pending'
                ORDER BY state, municipality_name
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (batch_index,),
            ).fetchone()
        else:
            row = execute(
                conn,
                """
                SELECT municipality_code
                FROM municipality_discovery
                WHERE batch_index = ?
                  AND status = 'pending'
                ORDER BY state, municipality_name
                LIMIT 1
                """,
                (batch_index,),
            ).fetchone()

        if not row:
            return None

        now = utc_now()
        lease_token = secrets.token_urlsafe(32)

        claimed = execute(
            conn,
            """
            UPDATE municipality_discovery
            SET status = 'running',
                attempts = attempts + 1,
                error = NULL,
                started_at = ?,
                completed_at = NULL,
                updated_at = ?,
                lease_token = ?
            WHERE municipality_code = ?
              AND batch_index = ?
              AND status = 'pending'
            RETURNING *
            """,
            (
                now,
                now,
                lease_token,
                row["municipality_code"],
                batch_index,
            ),
        ).fetchone()

        return dict(claimed) if claimed else None


def heartbeat_municipality(
    municipality_code: str,
    lease_token: str,
):
    """
    Renova a atividade de um município em execução.

    Somente o trabalhador que possui o token atual pode renovar
    a reserva.
    """
    code = _validate_municipality_code(municipality_code)
    token = _validate_lease_token(lease_token)
    now = utc_now()

    with db_session(immediate=True) as conn:
        row = execute(
            conn,
            """
            UPDATE municipality_discovery
            SET updated_at = ?
            WHERE municipality_code = ?
              AND status = 'running'
              AND lease_token = ?
            RETURNING *
            """,
            (now, code, token),
        ).fetchone()

    if not row:
        raise ValueError(
            "Reserva não encontrada, expirada ou token inválido."
        )

    return dict(row)


def mark_municipality_completed(
    municipality_code: str,
    lease_token: str,
):
    """
    Marca como concluído somente se o token corresponder à reserva atual.
    """
    code = _validate_municipality_code(municipality_code)
    token = _validate_lease_token(lease_token)
    now = utc_now()

    with db_session(immediate=True) as conn:
        row = execute(
            conn,
            """
            UPDATE municipality_discovery
            SET status = 'completed',
                error = NULL,
                completed_at = ?,
                updated_at = ?,
                lease_token = NULL
            WHERE municipality_code = ?
              AND status = 'running'
              AND lease_token = ?
            RETURNING *
            """,
            (now, now, code, token),
        ).fetchone()

    if not row:
        raise ValueError(
            "Reserva não encontrada ou token de execução inválido."
        )

    return dict(row)


def mark_municipality_failed(
    municipality_code: str,
    error: str,
    lease_token: str,
):
    """
    Registra falha somente se o token corresponder à reserva atual.
    """
    code = _validate_municipality_code(municipality_code)
    token = _validate_lease_token(lease_token)
    message = str(error).strip()[:2000]

    if not message:
        message = "Falha sem descrição."

    now = utc_now()

    with db_session(immediate=True) as conn:
        row = execute(
            conn,
            """
            UPDATE municipality_discovery
            SET status = 'failed',
                error = ?,
                completed_at = NULL,
                updated_at = ?,
                lease_token = NULL
            WHERE municipality_code = ?
              AND status = 'running'
              AND lease_token = ?
            RETURNING *
            """,
            (message, now, code, token),
        ).fetchone()

    if not row:
        raise ValueError(
            "Reserva não encontrada ou token de execução inválido."
        )

    return dict(row)


def retry_failed_municipality(
    municipality_code: str,
    max_attempts: int = MAX_MUNICIPALITY_ATTEMPTS,
):
    """
    Devolve um município com falha para pending.

    Preserva o histórico de tentativas e impede novas tentativas
    quando o limite configurado já foi atingido.
    """
    code = _validate_municipality_code(municipality_code)

    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts < 1
    ):
        raise ValueError(
            "O limite de tentativas deve ser um inteiro positivo."
        )

    now = utc_now()

    with db_session(immediate=True) as conn:
        row = execute(
            conn,
            """
            UPDATE municipality_discovery
            SET status = 'pending',
                error = NULL,
                started_at = NULL,
                completed_at = NULL,
                updated_at = ?,
                lease_token = NULL
            WHERE municipality_code = ?
              AND status = 'failed'
              AND attempts < ?
            RETURNING *
            """,
            (now, code, max_attempts),
        ).fetchone()

    if not row:
        raise ValueError(
            "Município não encontrado, não está com falha "
            "ou atingiu o limite de tentativas."
        )

    return dict(row)


def recover_interrupted_municipalities(
    batch_index: int | None = None,
    stale_after_seconds: int = 3600,
):
    """
    Recupera municípios sem atividade recente.

    Municípios que atingiram o limite de tentativas são marcados
    como failed. Os demais voltam para pending.

    O prazo considera updated_at, permitindo que o heartbeat
    renove a reserva. O token anterior é invalidado na recuperação.
    """
    if batch_index is not None:
        batch_index = _validate_batch_index(batch_index)

    if (
        isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, int)
        or stale_after_seconds < 0
    ):
        raise ValueError(
            "O prazo de recuperação deve ser um inteiro não negativo."
        )

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(seconds=stale_after_seconds)
    ).isoformat()

    now = utc_now()

    with db_session(immediate=True) as conn:
        if batch_index is None:
            cursor = execute(
                conn,
                """
                UPDATE municipality_discovery
                SET status = CASE
                        WHEN attempts >= ? THEN 'failed'
                        ELSE 'pending'
                    END,
                    error = CASE
                        WHEN attempts >= ?
                        THEN 'Limite de três tentativas atingido após interrupções.'
                        ELSE 'Execução interrompida; aguardando retomada.'
                    END,
                    started_at = NULL,
                    updated_at = ?,
                    lease_token = NULL
                WHERE status = 'running'
                  AND updated_at < ?
                """,
                (
                    MAX_MUNICIPALITY_ATTEMPTS,
                    MAX_MUNICIPALITY_ATTEMPTS,
                    now,
                    cutoff,
                ),
            )
        else:
            cursor = execute(
                conn,
                """
                UPDATE municipality_discovery
                SET status = CASE
                        WHEN attempts >= ? THEN 'failed'
                        ELSE 'pending'
                    END,
                    error = CASE
                        WHEN attempts >= ?
                        THEN 'Limite de três tentativas atingido após interrupções.'
                        ELSE 'Execução interrompida; aguardando retomada.'
                    END,
                    started_at = NULL,
                    updated_at = ?,
                    lease_token = NULL
                WHERE status = 'running'
                  AND batch_index = ?
                  AND updated_at < ?
                """,
                (
                    MAX_MUNICIPALITY_ATTEMPTS,
                    MAX_MUNICIPALITY_ATTEMPTS,
                    now,
                    batch_index,
                    cutoff,
                ),
            )

        return cursor.rowcount
        
