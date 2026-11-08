
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
