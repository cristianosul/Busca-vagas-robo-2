
from datetime import datetime, timezone

from app.repository import db_session, execute


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

    if not code or not name or len(state) != 2:
        raise ValueError("Dados de município inválidos.")

    if not code.isdigit():
        raise ValueError("O código IBGE deve conter somente números.")

    return code, name, state


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
                updated_at TEXT NOT NULL
            )
        """)

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

    with db_session(immediate=True) as conn:
        for code, name, state in records:
            execute(
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

        execute(
            conn,
            """
            INSERT INTO municipality_scheduler_state (
                state_id, current_batch, updated_at
            )
            VALUES (1, ?, ?)
            ON CONFLICT(state_id) DO UPDATE SET
                current_batch = excluded.current_batch,
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


def list_batch_municipalities(batch_index: int, status: str | None = None):
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
    Reserva atomicamente o próximo município pendente.

    O status só muda para running se ainda estiver pending,
    evitando que dois processos reservem o mesmo município.
    """
    batch_index = _validate_batch_index(batch_index)

    for _ in range(3):
        with db_session(immediate=True) as conn:
            row = execute(
                conn,
                """
                SELECT municipality_code
                FROM municipality_discovery
                WHERE batch_index = ? AND status = 'pending'
                ORDER BY state, municipality_name
                LIMIT 1
                """,
                (batch_index,),
            ).fetchone()

            if not row:
                return None

            now = utc_now()

            claimed = execute(
                conn,
                """
                UPDATE municipality_discovery
                SET status = 'running',
                    attempts = attempts + 1,
                    error = NULL,
                    started_at = ?,
                    completed_at = NULL,
                    updated_at = ?
                WHERE municipality_code = ?
                  AND batch_index = ?
                  AND status = 'pending'
                RETURNING *
                """,
                (
                    now,
                    now,
                    row["municipality_code"],
                    batch_index,
                ),
            ).fetchone()

            if claimed:
                return dict(claimed)

    return None


def mark_municipality_completed(municipality_code: str):
    """Marca como concluído um município que estava em execução."""
    code = str(municipality_code).strip()

    if not code:
        raise ValueError("Código IBGE inválido.")

    now = utc_now()

    with db_session(immediate=True) as conn:
        row = execute(
            conn,
            """
            UPDATE municipality_discovery
            SET status = 'completed',
                error = NULL,
                completed_at = ?,
                updated_at = ?
            WHERE municipality_code = ?
              AND status = 'running'
            RETURNING *
            """,
            (now, now, code),
        ).fetchone()

    if not row:
        raise ValueError(
            "Município não encontrado ou não estava em execução."
        )

    return dict(row)


def mark_municipality_failed(
    municipality_code: str,
    error: str,
):
    """Registra falha sem perder o histórico de tentativas."""
    code = str(municipality_code).strip()
    message = str(error).strip()[:2000]

    if not code:
        raise ValueError("Código IBGE inválido.")

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
                updated_at = ?
            WHERE municipality_code = ?
              AND status = 'running'
            RETURNING *
            """,
            (message, now, code),
        ).fetchone()

    if not row:
        raise ValueError(
            "Município não encontrado ou não estava em execução."
        )

    return dict(row)


def recover_interrupted_municipalities(batch_index: int | None = None):
    """
    Devolve municípios que ficaram running para pending.

    As tentativas anteriores são preservadas para auditoria.
    """
    if batch_index is not None:
        batch_index = _validate_batch_index(batch_index)

    now = utc_now()

    with db_session(immediate=True) as conn:
        if batch_index is None:
            cursor = execute(
                conn,
                """
                UPDATE municipality_discovery
                SET status = 'pending',
                    error = 'Execução interrompida; aguardando retomada.',
                    started_at = NULL,
                    updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
        else:
            cursor = execute(
                conn,
                """
                UPDATE municipality_discovery
                SET status = 'pending',
                    error = 'Execução interrompida; aguardando retomada.',
                    started_at = NULL,
                    updated_at = ?
                WHERE status = 'running'
                  AND batch_index = ?
                """,
                (now, batch_index),
            )

        return cursor.rowcount
  
