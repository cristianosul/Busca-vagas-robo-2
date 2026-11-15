
import sqlite3
from contextlib import contextmanager

import pytest

from app import municipality_scheduler as scheduler


@pytest.fixture
def scheduler_db(tmp_path, monkeypatch):
    """Fornece um banco SQLite isolado para cada teste."""
    database_path = tmp_path / "municipality_scheduler_test.db"

    @contextmanager
    def isolated_db_session(immediate=False):
        conn = sqlite3.connect(database_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")

        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")

            yield conn
            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

    monkeypatch.setattr(
        scheduler,
        "db_session",
        isolated_db_session,
    )

    scheduler.init_municipality_scheduler()

    return scheduler


def municipality(code, name="Município Teste", state="RS"):
    return {
        "code": str(code),
        "name": name,
        "state": state,
    }


def test_init_creates_scheduler_tables(scheduler_db):
    with scheduler_db.db_session() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        }

    assert "municipality_discovery" in tables
    assert "municipality_scheduler_state" in tables


def test_register_batch_saves_municipalities(scheduler_db):
    result = scheduler_db.register_municipality_batch(
        0,
        [
            municipality("4305108", "Caxias do Sul"),
            municipality("4314902", "Porto Alegre"),
        ],
    )

    assert result == {
        "batch_index": 0,
        "total": 2,
        "pending": 2,
        "running": 0,
        "completed": 0,
        "failed": 0,
    }

    assert scheduler_db.get_current_batch() == 0


def test_register_batch_does_not_duplicate_ibge_codes(scheduler_db):
    first = scheduler_db.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )

    second = scheduler_db.register_municipality_batch(
        1,
        [municipality("4305108", "Nome Diferente")],
    )

    assert first["total"] == 1
    assert second["total"] == 0

    with scheduler_db.db_session() as conn:
        rows = conn.execute(
            """
            SELECT municipality_code, municipality_name, batch_index
            FROM municipality_discovery
            """
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["municipality_code"] == "4305108"
    assert rows[0]["municipality_name"] == "Caxias do Sul"
    assert rows[0]["batch_index"] == 0


def test_register_batch_rejects_more_than_60_municipalities(
    scheduler_db,
):
    municipalities = [
        municipality(str(4300000 + index), f"Cidade {index}")
        for index in range(61)
    ]

    with pytest.raises(ValueError, match="60 municípios"):
        scheduler_db.register_municipality_batch(
            0,
            municipalities,
        )


def test_register_batch_rejects_duplicate_codes_in_same_batch(
    scheduler_db,
):
    municipalities = [
        municipality("4305108", "Caxias do Sul"),
        municipality("4305108", "Caxias duplicada"),
    ]

    with pytest.raises(ValueError, match="duplicados"):
        scheduler_db.register_municipality_batch(
            0,
            municipalities,
        )


def test_register_batch_rejects_invalid_municipality(scheduler_db):
    invalid = {
        "code": "4305108",
        "name": "Caxias do Sul",
        "state": "RIO GRANDE DO SUL",
    }

    with pytest.raises(ValueError, match="inválidos"):
        scheduler_db.register_municipality_batch(0, [invalid])


def test_claim_next_municipality_reserves_each_once(scheduler_db):
    scheduler_db.register_municipality_batch(
        0,
        [
            municipality("4305108", "Caxias do Sul"),
            municipality("4314902", "Porto Alegre"),
        ],
    )

    first = scheduler_db.claim_next_municipality(0)
    second = scheduler_db.claim_next_municipality(0)
    third = scheduler_db.claim_next_municipality(0)

    assert first is not None
    assert second is not None
    assert first["municipality_code"] != second["municipality_code"]
    assert first["status"] == "running"
    assert second["status"] == "running"
    assert third is None


def test_municipality_can_be_marked_completed(scheduler_db):
    scheduler_db.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )

    claimed = scheduler_db.claim_next_municipality(0)

    completed = scheduler_db.mark_municipality_completed(
        claimed["municipality_code"]
    )

    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None
    assert completed["error"] is None

    progress = scheduler_db.get_batch_progress(0)

    assert progress["completed"] == 1
    assert progress["pending"] == 0
    assert progress["running"] == 0


def test_cannot_complete_municipality_not_running(scheduler_db):
    scheduler_db.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )

    with pytest.raises(ValueError, match="não estava em execução"):
        scheduler_db.mark_municipality_completed("4305108")


def test_failed_municipality_keeps_attempt_count(scheduler_db):
    scheduler_db.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )

    claimed = scheduler_db.claim_next_municipality(0)

    failed = scheduler_db.mark_municipality_failed(
        claimed["municipality_code"],
        "Erro temporário de pesquisa",
    )

    assert failed["status"] == "failed"
    assert failed["attempts"] == 1
    assert failed["error"] == "Erro temporário de pesquisa"

    progress = scheduler_db.get_batch_progress(0)

    assert progress["failed"] == 1
    assert progress["running"] == 0


def test_failed_municipality_rejects_empty_error(scheduler_db):
    scheduler_db.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )

    scheduler_db.claim_next_municipality(0)

    failed = scheduler_db.mark_municipality_failed(
        "4305108",
        "   ",
    )

    assert failed["status"] == "failed"
    assert failed["error"] == "Falha sem descrição."


def test_recover_interrupted_municipality_preserves_attempts(
    scheduler_db,
):
    scheduler_db.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )

    first_claim = scheduler_db.claim_next_municipality(0)

    recovered = scheduler_db.recover_interrupted_municipalities(
        0,
        stale_after_seconds=0,
    )

    assert recovered == 1

    pending = scheduler_db.list_batch_municipalities(
        0,
        status="pending",
    )[0]

    assert pending["status"] == "pending"
    assert pending["attempts"] == 1
    assert pending["started_at"] is None

    second_claim = scheduler_db.claim_next_municipality(0)

    assert second_claim["municipality_code"] == (
        first_claim["municipality_code"]
    )
    assert second_claim["attempts"] == 2
    assert second_claim["status"] == "running"


def test_recovery_does_not_reset_completed_municipalities(scheduler_db):
    scheduler_db.register_municipality_batch(
        0,
        [
            municipality("4305108", "Caxias do Sul"),
            municipality("4314902", "Porto Alegre"),
        ],
    )

    claimed = scheduler_db.claim_next_municipality(0)

    scheduler_db.mark_municipality_completed(
        claimed["municipality_code"]
    )

    scheduler_db.claim_next_municipality(0)

    recovered = scheduler_db.recover_interrupted_municipalities(0)

    assert recovered == 1

    progress = scheduler_db.get_batch_progress(0)

    assert progress["completed"] == 1
    assert progress["pending"] == 1
    assert progress["running"] == 0


def test_recover_all_batches_when_batch_index_is_none(scheduler_db):
    scheduler_db.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )

    scheduler_db.register_municipality_batch(
        1,
        [municipality("4314902", "Porto Alegre")],
    )

    scheduler_db.claim_next_municipality(0)
    scheduler_db.claim_next_municipality(1)

    recovered = scheduler_db.recover_interrupted_municipalities(
        stale_after_seconds=0,
    )

    assert recovered == 2

    assert scheduler_db.get_batch_progress(0)["pending"] == 1
    assert scheduler_db.get_batch_progress(1)["pending"] == 1
    

def test_list_batch_municipalities_filters_by_status(scheduler_db):
    scheduler_db.register_municipality_batch(
        0,
        [
            municipality("4305108", "Caxias do Sul"),
            municipality("4314902", "Porto Alegre"),
        ],
    )

    scheduler_db.claim_next_municipality(0)

    running = scheduler_db.list_batch_municipalities(
        0,
        status="running",
    )

    pending = scheduler_db.list_batch_municipalities(
        0,
        status="pending",
    )

    assert len(running) == 1
    assert len(pending) == 1
    assert running[0]["status"] == "running"
    assert pending[0]["status"] == "pending"


def test_list_batch_rejects_invalid_status(scheduler_db):
    with pytest.raises(ValueError, match="Estado"):
        scheduler_db.list_batch_municipalities(
            0,
            status="unknown",
        )


@pytest.mark.parametrize(
    "batch_index",
    [-1, True, False, 1.5, "1", None],
)
def test_batch_functions_reject_invalid_index(
    scheduler_db,
    batch_index,
):
    with pytest.raises(ValueError):
        scheduler_db.get_batch_progress(batch_index)


def test_register_batch_rejects_empty_batch(scheduler_db):
    with pytest.raises(ValueError, match="vazio"):
        scheduler_db.register_municipality_batch(0, [])


def test_failed_municipality_cannot_be_completed_without_retry(
    scheduler_db,
):
    scheduler_db.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )

    scheduler_db.claim_next_municipality(0)

    scheduler_db.mark_municipality_failed(
        "4305108",
        "Falha simulada",
    )

    with pytest.raises(ValueError, match="não estava em execução"):
        scheduler_db.mark_municipality_completed("4305108")


def test_progress_for_unknown_batch_is_zero(scheduler_db):
    progress = scheduler_db.get_batch_progress(99)

    assert progress == {
        "batch_index": 99,
        "total": 0,
        "pending": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
    }


def test_current_batch_does_not_move_backwards(scheduler_db):
    scheduler_db.register_municipality_batch(
        2,
        [municipality("4305108", "Caxias do Sul")],
    )

    scheduler_db.register_municipality_batch(
        1,
        [municipality("4314902", "Porto Alegre")],
    )

    assert scheduler_db.get_current_batch() == 2


def test_duplicate_batch_does_not_change_current_batch(scheduler_db):
    scheduler_db.register_municipality_batch(
        2,
        [municipality("4305108", "Caxias do Sul")],
    )

    scheduler_db.register_municipality_batch(
        1,
        [municipality("4305108", "Nome diferente")],
    )

    assert scheduler_db.get_current_batch() == 2


def test_recent_running_municipality_is_not_recovered(scheduler_db):
    scheduler_db.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )

    scheduler_db.claim_next_municipality(0)

    recovered = scheduler_db.recover_interrupted_municipalities(
        0,
        stale_after_seconds=3600,
    )

    assert recovered == 0
    assert scheduler_db.get_batch_progress(0)["running"] == 1


def test_old_running_municipality_is_recovered(scheduler_db):
    scheduler_db.register_municipality_batch(
        0,
        [municipality("4305108", "Caxias do Sul")],
    )

    scheduler_db.claim_next_municipality(0)

    with scheduler_db.db_session(immediate=True) as conn:
        conn.execute(
            """
            UPDATE municipality_discovery
            SET started_at = '2000-01-01T00:00:00+00:00'
            WHERE municipality_code = '4305108'
            """
        )

    recovered = scheduler_db.recover_interrupted_municipalities(
        0,
        stale_after_seconds=3600,
    )

    assert recovered == 1
    assert scheduler_db.get_batch_progress(0)["pending"] == 1


@pytest.mark.parametrize(
    "stale_after_seconds",
    [-1, True, False, 1.5, "3600", None],
)
def test_recovery_rejects_invalid_stale_timeout(
    scheduler_db,
    stale_after_seconds,
):
    with pytest.raises(ValueError, match="prazo"):
        scheduler_db.recover_interrupted_municipalities(
            stale_after_seconds=stale_after_seconds,
        )
