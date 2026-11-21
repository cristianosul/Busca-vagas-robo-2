"""Testes de integração PostgreSQL para o scheduler municipal.

Este módulo só usa TEST_DATABASE_URL. Nunca utiliza DATABASE_URL como
fallback. A conexão é limitada a um PostgreSQL local e a um banco chamado
busca_vagas_test. Cada teste recebe um schema próprio, removido ao final.
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app import municipality_scheduler as scheduler
from app import repository


TEST_DATABASE_NAME = "busca_vagas_test"
ALLOWED_TEST_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres"}


def _test_database_url():
    """Obtém exclusivamente a URL do PostgreSQL temporário dos testes."""
    value = os.getenv("TEST_DATABASE_URL", "").strip()

    if not value:
        pytest.skip(
            "TEST_DATABASE_URL não configurada; "
            "os testes PostgreSQL foram ignorados."
        )

    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        database_name = parsed.path.lstrip("/")
        port = parsed.port
    except ValueError as exc:
        pytest.fail(f"TEST_DATABASE_URL inválida: {exc}")

    if parsed.scheme not in {"postgres", "postgresql"}:
        pytest.fail("TEST_DATABASE_URL deve usar PostgreSQL.")

    if hostname not in ALLOWED_TEST_HOSTS:
        pytest.fail(
            "Por segurança, os testes aceitam somente um PostgreSQL "
            "local do GitHub Actions. URLs externas, inclusive Neon, "
            "são bloqueadas."
        )

    if port not in {None, 5432}:
        pytest.fail("O PostgreSQL de testes deve usar a porta 5432.")

    # Impede parâmetros extras de conexão capazes de sobrescrever host,
    # porta ou outras opções definidas na URL local validada acima.
    if parsed.query or parsed.fragment:
        pytest.fail(
            "TEST_DATABASE_URL não pode conter parâmetros extras "
            "nem fragmentos."
        )

    if database_name != TEST_DATABASE_NAME:
        pytest.fail(
            f"O banco de testes deve se chamar '{TEST_DATABASE_NAME}'."
        )

    if not parsed.username or not parsed.password:
        pytest.fail("TEST_DATABASE_URL precisa informar usuário e senha.")

    return value


@pytest.fixture
def postgres_database(monkeypatch):
    """Cria schema isolado e direciona todas as conexões da aplicação a ele."""
    database_url = _test_database_url()
    schema_name = f"test_municipality_{uuid.uuid4().hex}"

    with psycopg.connect(
        database_url,
        sslmode="disable",
        connect_timeout=10,
        autocommit=True,
    ) as admin_conn:
        admin_conn.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
        )

    def isolated_connect():
        return psycopg.connect(
            database_url,
            sslmode="disable",
            connect_timeout=10,
            row_factory=dict_row,
            options=f"-c search_path={schema_name}",
        )

    # repository.db_session consulta repository.DATABASE_URL e repository.connect
    # em tempo de execução. O scheduler também mantém sua própria constante
    # DATABASE_URL importada, usada para escolher o SQL específico do PostgreSQL.
    monkeypatch.setattr(repository, "DATABASE_URL", database_url)
    monkeypatch.setattr(scheduler, "DATABASE_URL", database_url)
    monkeypatch.setattr(repository, "connect", isolated_connect)

    try:
        repository.init_db()
        scheduler.init_municipality_scheduler()

        yield {
            "schema": schema_name,
            "database_name": TEST_DATABASE_NAME,
        }
    finally:
        # Remove somente o schema aleatório criado por este teste.
        # Não remove tabelas, dados ou schemas compartilhados do banco.
        with psycopg.connect(
            database_url,
            sslmode="disable",
            connect_timeout=10,
            autocommit=True,
        ) as admin_conn:
            admin_conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )


def municipality(code, name, state="RS"):
    return {"code": code, "name": name, "state": state}


def register_two_municipalities(batch_index=0):
    return scheduler.register_municipality_batch(
        batch_index,
        [
            municipality("4305108", "Caxias do Sul"),
            municipality("4304605", "Canoas"),
        ],
    )


def make_claimed_municipality(
    code="4305108",
    name="Caxias do Sul",
    batch_index=0,
):
    scheduler.register_municipality_batch(
        batch_index,
        [municipality(code, name)],
    )
    claimed = scheduler.claim_next_municipality(batch_index)
    assert claimed is not None
    return claimed


def set_updated_at(code, value):
    with repository.db_session() as conn:
        repository.execute(
            conn,
            """
            UPDATE municipality_discovery
            SET updated_at = ?
            WHERE municipality_code = ?
            """,
            (value, code),
        )


# ---------------------------------------------------------------------------
# 1. Isolamento e inicialização
# ---------------------------------------------------------------------------


def test_postgres_uses_an_isolated_schema(postgres_database):
    with repository.db_session() as conn:
        row = repository.execute(
            conn, "SELECT current_schema() AS schema_name"
        ).fetchone()

    assert row["schema_name"] == postgres_database["schema"]


def test_postgres_init_adds_lease_token_to_legacy_table(postgres_database):
    """Migra tabela legada sem perder os municípios já registrados."""
    created_at = "2026-01-15T12:00:00+00:00"
    updated_at = "2026-01-15T12:30:00+00:00"

    with repository.db_session() as conn:
        conn.execute("DROP TABLE municipality_discovery")
        conn.execute("""
            CREATE TABLE municipality_discovery (
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
        repository.execute(
            conn,
            """
            INSERT INTO municipality_discovery (
                municipality_code, municipality_name, state, batch_index,
                status, attempts, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "4305108",
                "Caxias do Sul",
                "RS",
                7,
                "completed",
                2,
                created_at,
                updated_at,
            ),
        )

    # A inicialização deve detectar a tabela antiga e adicionar a coluna.
    scheduler.init_municipality_scheduler()

    with repository.db_session() as conn:
        columns = {
            row["column_name"]
            for row in repository.execute(
                conn,
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'municipality_discovery'
                """,
            ).fetchall()
        }
        row = repository.execute(
            conn,
            """
            SELECT municipality_code, municipality_name, state,
                   batch_index, status, attempts, created_at, updated_at,
                   lease_token
            FROM municipality_discovery
            WHERE municipality_code = ?
            """,
            ("4305108",),
        ).fetchone()

    assert "lease_token" in columns
    assert row is not None
    assert row["municipality_code"] == "4305108"
    assert row["municipality_name"] == "Caxias do Sul"
    assert row["state"] == "RS"
    assert row["batch_index"] == 7
    assert row["status"] == "completed"
    assert row["attempts"] == 2
    assert row["created_at"] == created_at
    assert row["updated_at"] == updated_at
    assert row["lease_token"] is None


def test_postgres_scheduler_initializes_empty_batch(postgres_database):
    assert scheduler.get_batch_progress(0) == {
        "batch_index": 0,
        "total": 0,
        "pending": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
    }
    assert scheduler.get_current_batch() is None


# ---------------------------------------------------------------------------
# 2. Registro, validação e idempotência dos lotes
# ---------------------------------------------------------------------------


def test_postgres_registers_municipality_batch(postgres_database):
    progress = register_two_municipalities()

    assert progress["total"] == 2
    assert progress["pending"] == 2
    assert progress["running"] == 0


def test_postgres_registers_current_batch(postgres_database):
    register_two_municipalities(batch_index=3)
    assert scheduler.get_current_batch() == 3


def test_postgres_current_batch_never_moves_backwards(postgres_database):
    scheduler.register_municipality_batch(
        3, [municipality("4305108", "Caxias do Sul")]
    )
    scheduler.register_municipality_batch(
        1, [municipality("4304605", "Canoas")]
    )

    assert scheduler.get_current_batch() == 3


def test_postgres_does_not_duplicate_municipality_codes(postgres_database):
    scheduler.register_municipality_batch(
        0, [municipality("4305108", "Caxias do Sul")]
    )
    progress = scheduler.register_municipality_batch(
        1, [municipality("4305108", "Nome diferente")]
    )

    assert progress["total"] == 0
    rows = scheduler.list_batch_municipalities(0)
    assert len(rows) == 1
    assert rows[0]["municipality_name"] == "Caxias do Sul"
    assert rows[0]["batch_index"] == 0


def test_postgres_rejects_duplicate_codes_inside_same_batch(postgres_database):
    with pytest.raises(ValueError):
        scheduler.register_municipality_batch(
            0,
            [
                municipality("4305108", "Caxias do Sul"),
                municipality("4305108", "Caxias duplicada"),
            ],
        )

    assert scheduler.get_batch_progress(0)["total"] == 0


def test_postgres_rejects_empty_batch(postgres_database):
    with pytest.raises(ValueError):
        scheduler.register_municipality_batch(0, [])


def test_postgres_rejects_batch_with_more_than_60_municipalities(
    postgres_database,
):
    municipalities = [
        municipality(f"{4300000 + index:07d}", f"Município {index}")
        for index in range(61)
    ]

    with pytest.raises(ValueError):
        scheduler.register_municipality_batch(0, municipalities)


@pytest.mark.parametrize("batch_index", [-1, True, 1.5, "1", None])
def test_postgres_rejects_invalid_batch_index(postgres_database, batch_index):
    with pytest.raises(ValueError):
        scheduler.register_municipality_batch(
            batch_index,
            [municipality("4305108", "Caxias do Sul")],
        )


@pytest.mark.parametrize(
    "item",
    [
        municipality("", "Caxias do Sul"),
        municipality("4305108", ""),
        municipality("4305108", "Caxias do Sul", ""),
        municipality("4305108", "Caxias do Sul", "R"),
        municipality("4305108", "Caxias do Sul", "123"),
        municipality("ABC5108", "Caxias do Sul"),
        municipality("430510", "Caxias do Sul"),
        municipality("43051088", "Caxias do Sul"),
    ],
)
def test_postgres_rejects_invalid_municipality_data(postgres_database, item):
    with pytest.raises(ValueError):
        scheduler.register_municipality_batch(0, [item])


# ---------------------------------------------------------------------------
# 3. Reserva e concorrência real com FOR UPDATE SKIP LOCKED
# ---------------------------------------------------------------------------


def test_postgres_claims_pending_municipality(postgres_database):
    register_two_municipalities()
    claimed = scheduler.claim_next_municipality(0)

    assert claimed is not None
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    assert claimed["lease_token"]
    assert claimed["municipality_code"] in {"4305108", "4304605"}


def test_postgres_claim_generates_unique_lease_tokens(postgres_database):
    register_two_municipalities()
    first = scheduler.claim_next_municipality(0)
    second = scheduler.claim_next_municipality(0)

    assert first is not None and second is not None
    assert first["lease_token"] != second["lease_token"]


def test_postgres_claims_each_municipality_only_once(postgres_database):
    register_two_municipalities()
    first = scheduler.claim_next_municipality(0)
    second = scheduler.claim_next_municipality(0)
    third = scheduler.claim_next_municipality(0)

    assert first is not None and second is not None
    assert first["municipality_code"] != second["municipality_code"]
    assert third is None


def test_postgres_claim_does_not_cross_batches(postgres_database):
    scheduler.register_municipality_batch(
        0, [municipality("4305108", "Caxias do Sul")]
    )
    scheduler.register_municipality_batch(
        1, [municipality("4304605", "Canoas")]
    )

    claim_batch_one = scheduler.claim_next_municipality(1)
    claim_batch_zero = scheduler.claim_next_municipality(0)

    assert claim_batch_one["municipality_code"] == "4304605"
    assert claim_batch_zero["municipality_code"] == "4305108"


def test_postgres_two_workers_claim_different_municipalities(postgres_database):
    register_two_municipalities()
    barrier = Barrier(3)

    def worker_claim():
        barrier.wait(timeout=10)
        return scheduler.claim_next_municipality(0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(worker_claim)
        second_future = executor.submit(worker_claim)
        barrier.wait(timeout=10)
        first = first_future.result(timeout=20)
        second = second_future.result(timeout=20)

    assert first is not None and second is not None
    assert first["municipality_code"] != second["municipality_code"]
    assert first["lease_token"] != second["lease_token"]

    progress = scheduler.get_batch_progress(0)
    assert progress["running"] == 2
    assert progress["pending"] == 0


def test_postgres_four_workers_claim_four_distinct_rows(postgres_database):
    scheduler.register_municipality_batch(
        0,
        [
            municipality("4305108", "Caxias do Sul"),
            municipality("4304605", "Canoas"),
            municipality("4309209", "Gravataí"),
            municipality("4314902", "Porto Alegre"),
        ],
    )
    barrier = Barrier(5)

    def worker_claim():
        barrier.wait(timeout=10)
        return scheduler.claim_next_municipality(0)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(worker_claim) for _ in range(4)]
        barrier.wait(timeout=10)
        claims = [future.result(timeout=20) for future in futures]

    codes = [claim["municipality_code"] for claim in claims]
    assert len(codes) == 4
    assert len(set(codes)) == 4
    assert all(claim["status"] == "running" for claim in claims)


def test_postgres_more_workers_than_rows_never_duplicate_a_claim(
    postgres_database,
):
    scheduler.register_municipality_batch(
        0,
        [
            municipality("4305108", "Caxias do Sul"),
            municipality("4304605", "Canoas"),
        ],
    )
    barrier = Barrier(5)

    def worker_claim():
        barrier.wait(timeout=10)
        return scheduler.claim_next_municipality(0)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(worker_claim) for _ in range(4)]
        barrier.wait(timeout=10)
        claims = [future.result(timeout=20) for future in futures]

    successful = [claim for claim in claims if claim is not None]
    claimed_codes = [claim["municipality_code"] for claim in successful]

    assert len(successful) == 2
    assert len(set(claimed_codes)) == 2


# ---------------------------------------------------------------------------
# 4. Heartbeat, tokens e propriedade da reserva
# ---------------------------------------------------------------------------


def test_postgres_heartbeat_accepts_current_token(postgres_database):
    claimed = make_claimed_municipality()
    renewed = scheduler.heartbeat_municipality(
        claimed["municipality_code"], claimed["lease_token"]
    )

    assert renewed["status"] == "running"
    assert renewed["lease_token"] == claimed["lease_token"]


def test_postgres_heartbeat_rejects_wrong_token(postgres_database):
    claimed = make_claimed_municipality()
    with pytest.raises(ValueError):
        scheduler.heartbeat_municipality(
            claimed["municipality_code"], "token-incorreto"
        )


def test_postgres_heartbeat_rejects_empty_token(postgres_database):
    claimed = make_claimed_municipality()
    with pytest.raises(ValueError):
        scheduler.heartbeat_municipality(claimed["municipality_code"], "")


def test_postgres_heartbeat_rejects_nonexistent_municipality(postgres_database):
    with pytest.raises(ValueError):
        scheduler.heartbeat_municipality("4305108", "token-inexistente")


def test_postgres_old_token_cannot_complete_municipality(postgres_database):
    claimed = make_claimed_municipality()
    with pytest.raises(ValueError):
        scheduler.mark_municipality_completed(
            claimed["municipality_code"], "token-antigo"
        )


def test_postgres_old_token_cannot_mark_municipality_failed(postgres_database):
    claimed = make_claimed_municipality()
    with pytest.raises(ValueError):
        scheduler.mark_municipality_failed(
            claimed["municipality_code"], "Falha simulada", "token-antigo"
        )


def test_postgres_completed_lease_rejects_old_heartbeat(postgres_database):
    claimed = make_claimed_municipality()
    scheduler.mark_municipality_completed(
        claimed["municipality_code"], claimed["lease_token"]
    )

    with pytest.raises(ValueError):
        scheduler.heartbeat_municipality(
            claimed["municipality_code"], claimed["lease_token"]
        )


# ---------------------------------------------------------------------------
# 5. Conclusão, falha e transições de estado
# ---------------------------------------------------------------------------


def test_postgres_marks_municipality_completed(postgres_database):
    claimed = make_claimed_municipality()
    completed = scheduler.mark_municipality_completed(
        claimed["municipality_code"], claimed["lease_token"]
    )

    assert completed["status"] == "completed"
    assert completed["lease_token"] is None
    assert completed["completed_at"] is not None
    progress = scheduler.get_batch_progress(0)
    assert progress["completed"] == 1
    assert progress["running"] == 0


def test_postgres_completed_municipality_cannot_be_claimed_again(postgres_database):
    claimed = make_claimed_municipality()
    scheduler.mark_municipality_completed(
        claimed["municipality_code"], claimed["lease_token"]
    )

    assert scheduler.claim_next_municipality(0) is None


def test_postgres_marks_municipality_failed(postgres_database):
    claimed = make_claimed_municipality()
    failed = scheduler.mark_municipality_failed(
        claimed["municipality_code"],
        "Erro de pesquisa simulado",
        claimed["lease_token"],
    )

    assert failed["status"] == "failed"
    assert failed["error"] == "Erro de pesquisa simulado"
    assert failed["lease_token"] is None


def test_postgres_truncates_failure_message_to_2000_characters(
    postgres_database,
):
    claimed = make_claimed_municipality()
    failed = scheduler.mark_municipality_failed(
        claimed["municipality_code"], "X" * 3000, claimed["lease_token"]
    )

    assert len(failed["error"]) == 2000


def test_postgres_empty_failure_message_uses_default(postgres_database):
    claimed = make_claimed_municipality()
    failed = scheduler.mark_municipality_failed(
        claimed["municipality_code"], "   ", claimed["lease_token"]
    )

    assert failed["error"] == "Falha sem descrição."


# ---------------------------------------------------------------------------
# 6. Retry e limite máximo de tentativas
# ---------------------------------------------------------------------------


def test_postgres_retries_failed_municipality(postgres_database):
    claimed = make_claimed_municipality()
    scheduler.mark_municipality_failed(
        claimed["municipality_code"], "Falha temporária", claimed["lease_token"]
    )
    retried = scheduler.retry_failed_municipality(claimed["municipality_code"])

    assert retried["status"] == "pending"
    assert retried["attempts"] == 1
    assert retried["error"] is None
    assert retried["lease_token"] is None


def test_postgres_preserves_attempt_count_after_retry(postgres_database):
    claimed = make_claimed_municipality()
    scheduler.mark_municipality_failed(
        claimed["municipality_code"], "Falha", claimed["lease_token"]
    )
    scheduler.retry_failed_municipality(claimed["municipality_code"])
    second = scheduler.claim_next_municipality(0)

    assert second["attempts"] == 2


def test_postgres_allows_exactly_three_attempts(postgres_database):
    claimed = make_claimed_municipality()

    for expected_attempt in (1, 2, 3):
        assert claimed["attempts"] == expected_attempt
        scheduler.mark_municipality_failed(
            claimed["municipality_code"],
            f"Falha {expected_attempt}",
            claimed["lease_token"],
        )
        if expected_attempt < 3:
            scheduler.retry_failed_municipality(claimed["municipality_code"])
            claimed = scheduler.claim_next_municipality(0)
            assert claimed is not None

    with pytest.raises(ValueError):
        scheduler.retry_failed_municipality(claimed["municipality_code"])

    progress = scheduler.get_batch_progress(0)
    assert progress["failed"] == 1
    assert progress["pending"] == 0


def test_postgres_cannot_retry_pending_municipality(postgres_database):
    scheduler.register_municipality_batch(
        0, [municipality("4305108", "Caxias do Sul")]
    )
    with pytest.raises(ValueError):
        scheduler.retry_failed_municipality("4305108")


@pytest.mark.parametrize("max_attempts", [0, -1, True, 1.5, "3", None])
def test_postgres_rejects_invalid_retry_limit(postgres_database, max_attempts):
    claimed = make_claimed_municipality()
    scheduler.mark_municipality_failed(
        claimed["municipality_code"], "Falha", claimed["lease_token"]
    )

    with pytest.raises(ValueError):
        scheduler.retry_failed_municipality(
            claimed["municipality_code"], max_attempts=max_attempts
        )


# ---------------------------------------------------------------------------
# 7. Recuperação de reservas interrompidas
# ---------------------------------------------------------------------------


def test_postgres_recovers_stale_running_municipality(postgres_database):
    claimed = make_claimed_municipality()
    old_timestamp = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    set_updated_at(claimed["municipality_code"], old_timestamp)

    recovered = scheduler.recover_interrupted_municipalities(
        batch_index=0, stale_after_seconds=60
    )

    assert recovered == 1
    rows = scheduler.list_batch_municipalities(0)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["lease_token"] is None
    assert rows[0]["attempts"] == 1


def test_postgres_recovery_invalidates_old_lease_token(postgres_database):
    claimed = make_claimed_municipality()
    old_timestamp = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    set_updated_at(claimed["municipality_code"], old_timestamp)
    scheduler.recover_interrupted_municipalities(
        batch_index=0, stale_after_seconds=60
    )

    with pytest.raises(ValueError):
        scheduler.heartbeat_municipality(
            claimed["municipality_code"], claimed["lease_token"]
        )


def test_postgres_recovery_does_not_touch_recent_heartbeat(postgres_database):
    make_claimed_municipality()
    recovered = scheduler.recover_interrupted_municipalities(
        batch_index=0, stale_after_seconds=3600
    )

    assert recovered == 0
    assert scheduler.get_batch_progress(0)["running"] == 1


def test_postgres_recovery_can_filter_by_batch(postgres_database):
    first = make_claimed_municipality(
        code="4305108", name="Caxias do Sul", batch_index=0
    )
    second = make_claimed_municipality(
        code="4304605", name="Canoas", batch_index=1
    )
    old_timestamp = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    set_updated_at(first["municipality_code"], old_timestamp)
    set_updated_at(second["municipality_code"], old_timestamp)

    recovered = scheduler.recover_interrupted_municipalities(
        batch_index=0, stale_after_seconds=60
    )

    assert recovered == 1
    assert scheduler.get_batch_progress(0)["pending"] == 1
    assert scheduler.get_batch_progress(1)["running"] == 1


def test_postgres_recovery_marks_third_interrupted_attempt_failed(
    postgres_database,
):
    claimed = make_claimed_municipality()

    for _ in range(2):
        scheduler.mark_municipality_failed(
            claimed["municipality_code"], "Falha simulada", claimed["lease_token"]
        )
        scheduler.retry_failed_municipality(claimed["municipality_code"])
        claimed = scheduler.claim_next_municipality(0)
        assert claimed is not None

    assert claimed["attempts"] == 3
    old_timestamp = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    set_updated_at(claimed["municipality_code"], old_timestamp)

    recovered = scheduler.recover_interrupted_municipalities(
        batch_index=0, stale_after_seconds=60
    )

    assert recovered == 1
    rows = scheduler.list_batch_municipalities(0)
    assert rows[0]["status"] == "failed"
    assert rows[0]["attempts"] == 3
    assert rows[0]["lease_token"] is None
    assert rows[0]["error"] == (
        "Limite de três tentativas atingido após interrupções."
    )


@pytest.mark.parametrize("seconds", [-1, True, 1.5, "60", None])
def test_postgres_rejects_invalid_recovery_timeout(postgres_database, seconds):
    with pytest.raises(ValueError):
        scheduler.recover_interrupted_municipalities(
            stale_after_seconds=seconds
        )


@pytest.mark.parametrize("batch_index", [-1, True, 1.5, "0"])
def test_postgres_recovery_rejects_invalid_batch_index(
    postgres_database, batch_index
):
    with pytest.raises(ValueError):
        scheduler.recover_interrupted_municipalities(
            batch_index=batch_index, stale_after_seconds=60
        )


# ---------------------------------------------------------------------------
# 8. Listagem e indicadores de progresso
# ---------------------------------------------------------------------------


def test_postgres_lists_municipalities_by_status(postgres_database):
    register_two_municipalities()
    claimed = scheduler.claim_next_municipality(0)

    running = scheduler.list_batch_municipalities(0, status="running")
    pending = scheduler.list_batch_municipalities(0, status="pending")

    assert len(running) == 1
    assert len(pending) == 1
    assert running[0]["municipality_code"] == claimed["municipality_code"]


def test_postgres_rejects_invalid_status_filter(postgres_database):
    register_two_municipalities()
    with pytest.raises(ValueError):
        scheduler.list_batch_municipalities(0, status="inexistente")


def test_postgres_progress_reflects_state_transitions(postgres_database):
    register_two_municipalities()
    first = scheduler.claim_next_municipality(0)
    scheduler.mark_municipality_completed(
        first["municipality_code"], first["lease_token"]
    )
    second = scheduler.claim_next_municipality(0)
    scheduler.mark_municipality_failed(
        second["municipality_code"], "Falha simulada", second["lease_token"]
    )

    progress = scheduler.get_batch_progress(0)
    assert progress == {
        "batch_index": 0,
        "total": 2,
        "pending": 0,
        "running": 0,
        "completed": 1,
        "failed": 1,
    }


# ---------------------------------------------------------------------------
# 9. Persistência de resultados brutos e prevenção de duplicidade
# ---------------------------------------------------------------------------


def save_result(
    *,
    municipality_code="4305108",
    municipality_name="Caxias do Sul",
    state="RS",
    category="vagas",
    query="auxiliar administrativo",
    url="https://empresa.example/vaga/123",
    payload=None,
):
    return repository.save_discovery_result(
        municipality_code=municipality_code,
        municipality_name=municipality_name,
        state=state,
        category=category,
        query=query,
        normalized_url=url,
        result=payload or {"title": "Auxiliar administrativo"},
    )


def test_postgres_saves_discovery_result(postgres_database):
    saved = save_result()

    assert saved["id"] is not None
    assert saved["municipality_code"] == "4305108"
    assert saved["normalized_url"] == "https://empresa.example/vaga/123"

    rows = repository.list_discovery_results(municipality_code="4305108")
    assert len(rows) == 1
    assert rows[0]["result"]["title"] == "Auxiliar administrativo"


def test_postgres_upserts_duplicate_without_creating_second_row(
    postgres_database,
):
    first = save_result(payload={"title": "Primeira versão"})
    second = save_result(payload={"title": "Versão atualizada"})

    assert first["id"] == second["id"]
    rows = repository.list_discovery_results(municipality_code="4305108")
    assert len(rows) == 1
    assert rows[0]["result"]["title"] == "Versão atualizada"
    assert rows[0]["first_seen_at"]
    assert rows[0]["last_seen_at"]


def test_postgres_preserves_first_seen_at_on_upsert(postgres_database):
    first = save_result(payload={"title": "Primeira versão"})
    first_row = repository.list_discovery_results(municipality_code="4305108")[0]

    second = save_result(payload={"title": "Segunda versão"})
    second_row = repository.list_discovery_results(municipality_code="4305108")[0]

    assert first["id"] == second["id"]
    assert second_row["first_seen_at"] == first_row["first_seen_at"]


def test_postgres_normalizes_tracking_parameters_before_deduplication(
    postgres_database,
):
    first = save_result(
        url="https://empresa.example/vaga/123?utm_source=google&fbclid=abc"
    )
    second = save_result(url="https://empresa.example/vaga/123")

    assert first["id"] == second["id"]
    assert first["normalized_url"] == second["normalized_url"]
    assert len(repository.list_discovery_results()) == 1


def test_postgres_keeps_different_queries_as_distinct_results(postgres_database):
    first = save_result(query="auxiliar administrativo")
    second = save_result(query="analista financeiro")

    assert first["id"] != second["id"]
    assert len(repository.list_discovery_results()) == 2


def test_postgres_keeps_different_municipalities_as_distinct_results(
    postgres_database,
):
    first = save_result()
    second = save_result(
        municipality_code="4304605",
        municipality_name="Canoas",
    )

    assert first["id"] != second["id"]
    assert len(repository.list_discovery_results()) == 2


def test_postgres_filters_discovery_results_by_state(postgres_database):
    save_result()
    save_result(
        municipality_code="3550308",
        municipality_name="São Paulo",
        state="SP",
        url="https://empresa.example/vaga/sp",
    )

    rows = repository.list_discovery_results(state="RS")
    assert len(rows) == 1
    assert rows[0]["state"] == "RS"


def test_postgres_rejects_invalid_discovery_municipality_code(postgres_database):
    with pytest.raises(ValueError):
        save_result(municipality_code="123")


def test_postgres_rejects_invalid_discovery_url(postgres_database):
    with pytest.raises(ValueError):
        save_result(url="javascript:alert(1)")


def test_postgres_raw_discovery_does_not_create_candidate(postgres_database):
    save_result()

    with repository.db_session() as conn:
        row = repository.execute(
            conn, "SELECT COUNT(*) AS total FROM candidates"
        ).fetchone()

    assert row["total"] == 0


def test_postgres_concurrent_duplicate_saves_return_same_id(postgres_database):
    barrier = Barrier(5)

    def save_concurrently():
        barrier.wait(timeout=10)
        return save_result(payload={"title": "Vaga concorrente"})

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(save_concurrently) for _ in range(4)]
        barrier.wait(timeout=10)
        results = [future.result(timeout=20) for future in futures]

    identifiers = {item["id"] for item in results}
    rows = repository.list_discovery_results(municipality_code="4305108")

    assert len(identifiers) == 1
    assert len(rows) == 1
