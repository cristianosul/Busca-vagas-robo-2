
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg.rows import dict_row

from app import municipality_scheduler as scheduler
from app import repository


def _test_database_url():
    """
    Exige uma URL explícita para o PostgreSQL descartável.
    Nunca usa DATABASE_URL como alternativa.
    """
    value = os.getenv("TEST_DATABASE_URL", "").strip()

    if not value:
        pytest.skip(
            "TEST_DATABASE_URL não configurada; "
            "testes PostgreSQL não serão executados."
        )

    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    database_name = parsed.path.lstrip("/")

    allowed_hosts = {
        "localhost",
        "127.0.0.1",
        "::1",
        "postgres",
    }

    if hostname not in allowed_hosts:
        pytest.fail(
            "Por segurança, os testes PostgreSQL só aceitam "
            "um servidor local do GitHub Actions."
        )

    if database_name != "busca_vagas_test":
        pytest.fail(
            "O banco de testes deve se chamar "
            "'busca_vagas_test'."
        )

    if not parsed.username or not parsed.password:
        pytest.fail(
            "TEST_DATABASE_URL precisa informar usuário e senha."
        )

    return value


@pytest.fixture
def postgres_database(monkeypatch):
    """
    Cria um schema exclusivo para cada teste.

    Todas as conexões da aplicação são direcionadas ao schema
    temporário. Ao final, o schema é removido.
    """
    database_url = _test_database_url()
    schema_name = f"test_municipality_{uuid.uuid4().hex}"

    with psycopg.connect(
        database_url,
        sslmode="disable",
        autocommit=True,
    ) as admin_conn:
        admin_conn.execute(
            f'CREATE SCHEMA "{schema_name}"'
        )

    original_connect = repository.connect

    def isolated_connect():
        conn = psycopg.connect(
            database_url,
            sslmode="disable",
            connect_timeout=10,
            row_factory=dict_row,
        )
        conn.execute(
            f'SET search_path TO "{schema_name}"'
        )
        return conn

    monkeypatch.setattr(
        repository,
        "DATABASE_URL",
        database_url,
    )
    monkeypatch.setattr(
        scheduler,
        "DATABASE_URL",
        database_url,
    )
    monkeypatch.setattr(
        repository,
        "connect",
        isolated_connect,
    )

    try:
        repository.init_db()
        scheduler.init_municipality_scheduler()

        yield {
            "schema": schema_name,
            "database_url": database_url,
        }

    finally:
        # Restaura a função original antes da limpeza.
        monkeypatch.setattr(
            repository,
            "connect",
            original_connect,
        )

        with psycopg.connect(
            database_url,
            sslmode="disable",
            autocommit=True,
        ) as admin_conn:
            admin_conn.execute(
                f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'
            )


def municipality(code, name, state="RS"):
    return {
        "code": code,
        "name": name,
        "state": state,
    }


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
    with repository.db_session(immediate=True) as conn:
        repository.execute(
            conn,
            """
            UPDATE municipality_discovery
            SET updated_at = ?
            WHERE municipality_code = ?
            """,
            (value, code),
        )


# ---------------------------------------------------------------------
# 1. Inicialização e registro de municípios
# ---------------------------------------------------------------------


def test_postgres_scheduler_initializes(postgres_database):
    progress = scheduler.get_batch_progress(0)

    assert progress == {
        "batch_index": 0,
        "total": 0,
        "pending": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
    }


def test_postgres_registers_municipality_batch(postgres_database):
    progress = register_two_municipalities()

    assert progress["total"] == 2
    assert progress["pending"] == 2
    assert progress["running"] == 0


def test_postgres_registers_current_batch(postgres_database):
    register_two_municipalities(batch_index=3)

    assert scheduler.get_current_batch() == 3


def test_postgres_does_not_duplicate_municipality_codes(
    postgres_database,
):
    scheduler.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )

    progress = scheduler.register_municipality_batch(
        1,
        [municipality("4305108", "Nome diferente")],
    )

    assert progress["total"] == 0

    rows = scheduler.list_batch_municipalities(0)

    assert len(rows) == 1
    assert rows[0]["municipality_name"] == "Caxias do Sul"


def test_postgres_rejects_duplicate_codes_inside_same_batch(
    postgres_database,
):
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
        municipality(
            f"{4300000 + index:07d}",
            f"Município {index}",
        )
        for index in range(61)
    ]

    with pytest.raises(ValueError):
        scheduler.register_municipality_batch(
            0,
            municipalities,
        )


@pytest.mark.parametrize(
    "batch_index",
    [-1, True, 1.5, "1", None],
)
def test_postgres_rejects_invalid_batch_index(
    postgres_database,
    batch_index,
):
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
def test_postgres_rejects_invalid_municipality_data(
    postgres_database,
    item,
):
    with pytest.raises(ValueError):
        scheduler.register_municipality_batch(0, [item])


# ---------------------------------------------------------------------
# 2. Reserva de municípios e controle de estado
# ---------------------------------------------------------------------


def test_postgres_claims_pending_municipality(postgres_database):
    register_two_municipalities()

    claimed = scheduler.claim_next_municipality(0)

    assert claimed is not None
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    assert claimed["lease_token"]
    assert claimed["municipality_code"] in {
        "4305108",
        "4304605",
    }


def test_postgres_claim_generates_unique_lease_tokens(
    postgres_database,
):
    register_two_municipalities()

    first = scheduler.claim_next_municipality(0)
    second = scheduler.claim_next_municipality(0)

    assert first is not None
    assert second is not None
    assert first["lease_token"] != second["lease_token"]


def test_postgres_claims_each_municipality_only_once(
    postgres_database,
):
    register_two_municipalities()

    first = scheduler.claim_next_municipality(0)
    second = scheduler.claim_next_municipality(0)
    third = scheduler.claim_next_municipality(0)

    assert first is not None
    assert second is not None
    assert first["municipality_code"] != second["municipality_code"]
    assert third is None


def test_postgres_returns_none_when_batch_has_no_pending_rows(
    postgres_database,
):
    register_two_municipalities()

    first = scheduler.claim_next_municipality(0)
    second = scheduler.claim_next_municipality(0)

    assert first is not None
    assert second is not None
    assert scheduler.claim_next_municipality(0) is None


def test_postgres_claim_increments_attempts(postgres_database):
    claimed = make_claimed_municipality()

    assert claimed["attempts"] == 1

    scheduler.mark_municipality_failed(
        claimed["municipality_code"],
        "Falha simulada",
        claimed["lease_token"],
    )

    scheduler.retry_failed_municipality(
        claimed["municipality_code"]
    )

    second_claim = scheduler.claim_next_municipality(0)

    assert second_claim["attempts"] == 2


def test_postgres_claim_does_not_cross_batches(postgres_database):
    scheduler.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )
    scheduler.register_municipality_batch(
        1,
        [municipality("4304605", "Canoas")],
    )

    claimed = scheduler.claim_next_municipality(1)

    assert claimed["municipality_code"] == "4304605"
    assert scheduler.claim_next_municipality(0)["municipality_code"] == (
        "4305108"
    )


# ---------------------------------------------------------------------
# 3. Concorrência real no PostgreSQL
# ---------------------------------------------------------------------


def test_postgres_two_workers_claim_different_municipalities(
    postgres_database,
):
    register_two_municipalities()

    barrier = Barrier(3)

    def worker_claim():
        barrier.wait(timeout=10)
        return scheduler.claim_next_municipality(0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(worker_claim)
        second_future = executor.submit(worker_claim)

        # Libera os dois workers praticamente ao mesmo tempo.
        barrier.wait(timeout=10)

        first = first_future.result(timeout=20)
        second = second_future.result(timeout=20)

    assert first is not None
    assert second is not None

    assert first["municipality_code"] != second["municipality_code"]
    assert first["lease_token"] != second["lease_token"]

    progress = scheduler.get_batch_progress(0)

    assert progress["running"] == 2
    assert progress["pending"] == 0


def test_postgres_concurrent_workers_do_not_claim_same_row(
    postgres_database,
):
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
        futures = [
            executor.submit(worker_claim)
            for _ in range(4)
        ]

        barrier.wait(timeout=10)

        claims = [
            future.result(timeout=20)
            for future in futures
        ]

    codes = [claim["municipality_code"] for claim in claims]

    assert len(codes) == 4
    assert len(set(codes)) == 4


# ---------------------------------------------------------------------
# 4. Heartbeat e validade do lease
# ---------------------------------------------------------------------


def test_postgres_heartbeat_accepts_current_token(postgres_database):
    claimed = make_claimed_municipality()

    renewed = scheduler.heartbeat_municipality(
        claimed["municipality_code"],
        claimed["lease_token"],
    )

    assert renewed["status"] == "running"
    assert renewed["lease_token"] == claimed["lease_token"]


def test_postgres_heartbeat_rejects_wrong_token(postgres_database):
    claimed = make_claimed_municipality()

    with pytest.raises(ValueError):
        scheduler.heartbeat_municipality(
            claimed["municipality_code"],
            "token-incorreto",
        )


def test_postgres_heartbeat_rejects_empty_token(postgres_database):
    claimed = make_claimed_municipality()

    with pytest.raises(ValueError):
        scheduler.heartbeat_municipality(
            claimed["municipality_code"],
            "",
        )


def test_postgres_heartbeat_rejects_nonexistent_municipality(
    postgres_database,
):
    with pytest.raises(ValueError):
        scheduler.heartbeat_municipality(
            "4305108",
            "token-inexistente",
        )


def test_postgres_old_token_cannot_complete_municipality(
    postgres_database,
):
    claimed = make_claimed_municipality()

    with pytest.raises(ValueError):
        scheduler.mark_municipality_completed(
            claimed["municipality_code"],
            "token-antigo",
        )


def test_postgres_old_token_cannot_mark_municipality_failed(
    postgres_database,
):
    claimed = make_claimed_municipality()

    with pytest.raises(ValueError):
        scheduler.mark_municipality_failed(
            claimed["municipality_code"],
            "Falha simulada",
            "token-antigo",
        )


def test_postgres_old_token_cannot_renew_lease(postgres_database):
    claimed = make_claimed_municipality()

    scheduler.mark_municipality_completed(
        claimed["municipality_code"],
        claimed["lease_token"],
    )

    with pytest.raises(ValueError):
        scheduler.heartbeat_municipality(
            claimed["municipality_code"],
            claimed["lease_token"],
        )


# ---------------------------------------------------------------------
# 5. Conclusão e falha de execução
# ---------------------------------------------------------------------


def test_postgres_marks_municipality_completed(postgres_database):
    claimed = make_claimed_municipality()

    completed = scheduler.mark_municipality_completed(
        claimed["municipality_code"],
        claimed["lease_token"],
    )

    assert completed["status"] == "completed"
    assert completed["lease_token"] is None
    assert completed["completed_at"] is not None

    progress = scheduler.get_batch_progress(0)

    assert progress["completed"] == 1
    assert progress["running"] == 0


def test_postgres_completed_municipality_cannot_be_claimed_again(
    postgres_database,
):
    claimed = make_claimed_municipality()

    scheduler.mark_municipality_completed(
        claimed["municipality_code"],
        claimed["lease_token"],
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


def test_postgres_truncates_failure_message(postgres_database):
    claimed = make_claimed_municipality()

    failed = scheduler.mark_municipality_failed(
        claimed["municipality_code"],
        "X" * 3000,
        claimed["lease_token"],
    )

    assert len(failed["error"]) == 2000


def test_postgres_empty_failure_message_uses_default(
    postgres_database,
):
    claimed = make_claimed_municipality()

    failed = scheduler.mark_municipality_failed(
        claimed["municipality_code"],
        "   ",
        claimed["lease_token"],
    )

    assert failed["error"] == "Falha sem descrição."


# ---------------------------------------------------------------------
# 6. Retry e limite de três tentativas
# ---------------------------------------------------------------------


def test_postgres_retries_failed_municipality(postgres_database):
    claimed = make_claimed_municipality()

    scheduler.mark_municipality_failed(
        claimed["municipality_code"],
        "Falha temporária",
        claimed["lease_token"],
    )

    retried = scheduler.retry_failed_municipality(
        claimed["municipality_code"]
    )

    assert retried["status"] == "pending"
    assert retried["attempts"] == 1
    assert retried["error"] is None
    assert retried["lease_token"] is None


def test_postgres_preserves_attempt_count_after_retry(
    postgres_database,
):
    claimed = make_claimed_municipality()

    scheduler.mark_municipality_failed(
        claimed["municipality_code"],
        "Falha",
        claimed["lease_token"],
    )

    scheduler.retry_failed_municipality(
        claimed["municipality_code"]
    )

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
            scheduler.retry_failed_municipality(
                claimed["municipality_code"]
            )
            claimed = scheduler.claim_next_municipality(0)
            assert claimed is not None

    with pytest.raises(ValueError):
        scheduler.retry_failed_municipality(
            claimed["municipality_code"]
        )

    progress = scheduler.get_batch_progress(0)

    assert progress["failed"] == 1
    assert progress["pending"] == 0


def test_postgres_cannot_retry_pending_municipality(postgres_database):
    scheduler.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )

    with pytest.raises(ValueError):
        scheduler.retry_failed_municipality("4305108")


@pytest.mark.parametrize(
    "max_attempts",
    [0, -1, True, 1.5, "3", None],
)
def test_postgres_rejects_invalid_retry_limit(
    postgres_database,
    max_attempts,
):
    claimed = make_claimed_municipality()

    scheduler.mark_municipality_failed(
        claimed["municipality_code"],
        "Falha",
        claimed["lease_token"],
    )

    with pytest.raises(ValueError):
        scheduler.retry_failed_municipality(
            claimed["municipality_code"],
            max_attempts=max_attempts,
        )


# ---------------------------------------------------------------------
# 7. Recuperação de execuções interrompidas
# ---------------------------------------------------------------------


def test_postgres_recovers_stale_running_municipality(
    postgres_database,
):
    claimed = make_claimed_municipality()

    old_timestamp = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()

    set_updated_at(
        claimed["municipality_code"],
        old_timestamp,
    )

    recovered = scheduler.recover_interrupted_municipalities(
        batch_index=0,
        stale_after_seconds=60,
    )

    assert recovered == 1

    rows = scheduler.list_batch_municipaliti
