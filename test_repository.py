
import pytest

import app.repository as repository
from app.models import CandidateEvidence, CandidateIn, SourceCreate


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(
        repository,
        "DB_PATH",
        tmp_path / "test_repository.db",
    )
    repository.init_db()


def make_candidate(url="https://example.com/jobs/123"):
    return CandidateIn(
        title="Auxiliar administrativo",
        company="Empresa de Teste",
        location="Brasil",
        mode="Não informado",
        source_name="Teste interno",
        source_url="https://example.com/careers",
        original_job_url=url,
        application_url=None,
    )


def make_source(source_id="empresa-teste"):
    return SourceCreate(
        source_id=source_id,
        name="Empresa de Teste",
        source_type="company",
        base_url="https://example.com",
        authorization="official_public_page",
        enabled=False,
        notes="Fonte fictícia para testes",
    )


def test_normalize_job_url_removes_tracking_parameters():
    result = repository.normalize_job_url(
        "HTTPS://Example.com/jobs/123/?utm_source=test&fbclid=abc"
    )

    assert result == "https://example.com/jobs/123"


def test_normalize_job_url_preserves_meaningful_parameters():
    result = repository.normalize_job_url(
        "https://example.com/jobs/123?id=456"
    )

    assert result == "https://example.com/jobs/123?id=456"


def test_normalize_job_url_rejects_invalid_scheme():
    with pytest.raises(ValueError):
        repository.normalize_job_url(
            "ftp://example.com/jobs/123"
        )


def test_save_candidate_inserts_candidate(isolated_db):
    result = repository.save_candidate(make_candidate())

    assert result["id"] is not None
    assert result["status"] == "review"
    assert result["score"] == 0


def test_save_candidate_blocks_duplicate(isolated_db):
    candidate = make_candidate()

    first = repository.save_candidate(candidate)
    second = repository.save_candidate(candidate)

    assert first["id"] == second["id"]
    assert second["status"] == "duplicate"
    assert second["existing_status"] == "review"

    with repository.connect() as conn:
        row = repository.execute(
            conn,
            "SELECT COUNT(*) AS total FROM candidates",
        ).fetchone()

    assert row["total"] == 1


def test_save_candidate_uses_evidence_score(isolated_db):
    evidence = CandidateEvidence(
        company_verified=True,
        original_page_verified=True,
        application_link_verified=True,
        job_content_verified=True,
        date_verified=True,
    )

    result = repository.save_candidate(
        make_candidate(),
        evidence,
    )

    assert result["score"] == 100
    assert result["status"] == "approved"


def test_list_candidates_returns_saved_candidate(isolated_db):
    repository.save_candidate(make_candidate())

    results = repository.list_candidates()

    assert len(results) == 1
    assert results[0]["title"] == "Auxiliar administrativo"
    assert results[0]["company"] == "Empresa de Teste"


def test_list_candidates_filters_by_status(isolated_db):
    repository.save_candidate(make_candidate())

    results = repository.list_candidates(status="review")

    assert len(results) == 1
    assert results[0]["status"] == "review"

    approved = repository.list_candidates(status="approved")

    assert approved == []


def test_save_source_inserts_source(isolated_db):
    result = repository.save_source(make_source())

    assert result["source_id"] == "empresa-teste"
    assert result["enabled"] is False


def test_save_source_rejects_duplicate(isolated_db):
    source = make_source()

    repository.save_source(source)

    with pytest.raises(ValueError):
        repository.save_source(source)


def test_get_source_record_returns_source(isolated_db):
    repository.save_source(make_source())

    result = repository.get_source_record("empresa-teste")

    assert result is not None
    assert result["name"] == "Empresa de Teste"


def test_get_source_record_returns_none_for_missing_source(isolated_db):
    result = repository.get_source_record("inexistente")

    assert result is None


def test_list_sources_returns_registered_sources(isolated_db):
    repository.save_source(make_source())

    results = repository.list_sources()

    assert len(results) == 1
    assert results[0]["source_id"] == "empresa-teste"


def test_update_source_status_changes_enabled(isolated_db):
    repository.save_source(make_source())

    result = repository.update_source_status(
        "empresa-teste",
        True,
    )

    assert result is not None
    assert result["enabled"] is True


def test_update_source_status_returns_none_for_missing_source(
    isolated_db,
):
    result = repository.update_source_status(
        "inexistente",
        True,
    )

    assert result is None


def test_list_candidates_preserves_all_evidence(isolated_db):
    evidence = CandidateEvidence(
        company_verified=True,
        original_page_verified=True,
        application_link_verified=True,
        job_content_verified=True,
        date_verified=True,
    )

    repository.save_candidate(make_candidate(), evidence)

    results = repository.list_candidates()

    assert len(results) == 1
    assert results[0]["score"] == 100
    assert results[0]["status"] == "approved"

    saved_evidence = results[0]["evidence"]

    assert saved_evidence["company_verified"] is True
    assert saved_evidence["original_page_verified"] is True
    assert saved_evidence["application_link_verified"] is True
    assert saved_evidence["job_content_verified"] is True
    assert saved_evidence["date_verified"] is True


def test_list_candidates_preserves_company_relationships(isolated_db):
    data = make_candidate().model_dump()
    data.update({
        "advertiser_company": "Agência Anunciante",
        "hiring_company": "Empresa Contratante",
        "intermediary_name": "Consultoria Intermediária",
    })

    candidate = CandidateIn(**data)

    repository.save_candidate(candidate)

    result = repository.list_candidates()[0]

    assert result["advertiser_company"] == "Agência Anunciante"
    assert result["hiring_company"] == "Empresa Contratante"
    assert result["intermediary_name"] == "Consultoria Intermediária"


def test_list_sources_filters_enabled_sources(isolated_db):
    repository.save_source(make_source("empresa-1"))
    repository.save_source(make_source("empresa-2"))

    repository.update_source_status("empresa-2", True)

    enabled = repository.list_sources(enabled_only=True)

    assert len(enabled) == 1
    assert enabled[0]["source_id"] == "empresa-2"
    assert enabled[0]["enabled"] is True


def test_candidate_insert_failure_rolls_back(isolated_db, monkeypatch):
    original_execute = repository.execute

    def failing_execute(conn, sql, params=()):
        if "INSERT INTO candidates" in sql:
            raise RuntimeError("Falha simulada na gravação")

        return original_execute(conn, sql, params)

    monkeypatch.setattr(repository, "execute", failing_execute)

    with pytest.raises(RuntimeError, match="Falha simulada"):
        repository.save_candidate(make_candidate())

    monkeypatch.setattr(repository, "execute", original_execute)

    with repository.connect() as conn:
        row = repository.execute(
            conn,
            "SELECT COUNT(*) AS total FROM candidates",
        ).fetchone()

    assert row["total"] == 0


def test_init_db_migrates_legacy_database(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"

    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(repository, "DB_PATH", db_path)

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT,
                mode TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                original_job_url TEXT NOT NULL,
                application_url TEXT,
                score INTEGER NOT NULL,
                status TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE sources (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                base_url TEXT NOT NULL,
                "authorization" TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            INSERT INTO candidates (
                title, company, location, mode, source_name,
                source_url, original_job_url, application_url,
                score, status, reasons_json, payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "Vaga antiga",
            "Empresa antiga",
            "Brasil",
            "Não informado",
            "Fonte antiga",
            "https://example.com/careers",
            "https://example.com/jobs/123",
            None,
            0,
            "review",
            "[]",
            "{}",
            "2026-01-01T00:00:00+00:00",
        ))

    repository.init_db()

    with repository.connect() as conn:
        candidate_columns = repository._get_table_columns(
            conn, "candidates"
        )
        source_columns = repository._get_table_columns(
            conn, "sources"
        )

        row = repository.execute(
            conn,
            "SELECT normalized_url FROM candidates WHERE id = ?",
            (1,),
        ).fetchone()

    assert "normalized_url" in candidate_columns
    assert "evidence_json" in candidate_columns
    assert "official_site_url" in source_columns
    assert "authorization_evidence_url" in source_columns
    assert row["normalized_url"] == "https://example.com/jobs/123"


def test_init_db_stops_on_duplicate_normalized_urls(
    tmp_path,
    monkeypatch,
):
    import sqlite3

    db_path = tmp_path / "duplicate_legacy.db"

    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(repository, "DB_PATH", db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT,
                mode TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                original_job_url TEXT NOT NULL,
                application_url TEXT,
                score INTEGER NOT NULL,
                status TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE sources (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                base_url TEXT NOT NULL,
                "authorization" TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        for url in (
            "https://example.com/jobs/123?utm_source=google",
            "https://example.com/jobs/123",
        ):
            conn.execute("""
                INSERT INTO candidates (
                    title, company, location, mode, source_name,
                    source_url, original_job_url, application_url,
                    score, status, reasons_json, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "Vaga antiga",
                "Empresa antiga",
                "Brasil",
                "Não informado",
                "Fonte antiga",
                "https://example.com/careers",
                url,
                None,
                0,
                "review",
                "[]",
                "{}",
                "2026-01-01T00:00:00+00:00",
            ))

    with pytest.raises(RuntimeError, match="URLs normalizadas duplicadas"):
        repository.init_db()

    with sqlite3.connect(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM candidates"
        ).fetchone()[0]

    assert total == 2
    
