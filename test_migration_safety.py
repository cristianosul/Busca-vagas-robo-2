
import pytest

import app.repository as repository


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(
        repository,
        "DB_PATH",
        tmp_path / "test_migration.db",
    )
    repository.init_db()


def insert_candidate(conn, normalized_url):
    conn.execute(
        """
        INSERT INTO candidates (
            title,
            company,
            location,
            mode,
            source_name,
            source_url,
            original_job_url,
            normalized_url,
            application_url,
            score,
            status,
            reasons_json,
            payload_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Auxiliar administrativo",
            "Empresa de Teste",
            "Brasil",
            "Não informado",
            "Teste interno",
            "https://example.com/careers",
            "https://example.com/jobs/123",
            normalized_url,
            None,
            0,
            "review",
            "[]",
            "{}",
            "2026-11-07T00:00:00+00:00",
        ),
    )



def test_migration_does_not_silently_delete_existing_duplicates(
    isolated_db,
):
    normalized_url = "https://example.com/jobs/123"

    with repository.connect() as conn:
        # Simula uma tabela antiga, anterior ao índice único.
        conn.execute("DROP INDEX IF EXISTS idx_candidates_normalized_url")
        conn.execute("DROP INDEX IF EXISTS ux_candidates_normalized_url")

        insert_candidate(conn, normalized_url)
        insert_candidate(conn, normalized_url)

    # A migração deve interromper-se ao encontrar duplicidades,
    # sem apagar registros silenciosamente.
    with pytest.raises(RuntimeError):
        repository.init_db()

    with repository.connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM candidates
            WHERE normalized_url = ?
            """,
            (normalized_url,),
        ).fetchone()

    assert row["total"] == 2
    
