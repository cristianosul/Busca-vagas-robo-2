"""Testes SQLite isolados para fila e persistência de extrações do Robô 2."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from app import repository as repo
from app.models import (
    CandidateEvidence,
    CandidateIn,
    EvidenceRecord,
    ExtractedJobData,
)


BASE_URL = "https://empresa.example/vagas/123"


@pytest.fixture
def isolated_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "DATABASE_URL", "")
    monkeypatch.setattr(repo, "DB_PATH", tmp_path / "repository-test.db")
    repo.init_db()
    return repo


def add_discovery(repository, *, url=BASE_URL, result=None):
    return repository.save_discovery_result(
        municipality_code="4305108",
        municipality_name="Caxias do Sul",
        state="RS",
        category="job_search",
        query="auxiliar administrativo",
        normalized_url=url,
        result=result or {"title": "Vaga encontrada", "url": url},
    )


def make_extracted(**overrides):
    data = {
        "title": "Auxiliar administrativo",
        "company": None,
        "company_hint": "Empresa Exemplo",
        "description": "Descrição inicial da vaga.",
        "page_url": BASE_URL,
        "extraction_method": "json_ld",
        "missing_fields": ["company"],
    }
    data.update(overrides)
    return ExtractedJobData(**data)


def make_candidate(**overrides):
    data = {
        "title": "Auxiliar administrativo",
        "company": "Empresa Exemplo",
        "source_name": "Página oficial",
        "source_url": "https://empresa.example/carreiras",
        "original_job_url": BASE_URL,
        "description": "Descrição completa da oportunidade.",
        "requirements": "Ensino médio completo.",
    }
    data.update(overrides)
    return CandidateIn(**data)


def verified_company_evidence(result="verified", checked_at=None):
    checked_at = checked_at or datetime.now(timezone.utc)
    record = EvidenceRecord(
        criterion="company_verified",
        evidence_url="https://empresa.example/sobre",
        method="manual_review",
        checked_at=checked_at,
        result=result,
    )
    return CandidateEvidence(
        company_verified=(result == "verified"),
        evidence_records=[record],
    )


def test_discovery_is_saved_without_creating_candidate(isolated_repository):
    saved = add_discovery(isolated_repository)

    assert saved["id"] > 0
    assert isolated_repository.list_discovery_results()[0]["result"]["title"] == "Vaga encontrada"
    assert isolated_repository.list_candidates() == []


def test_changed_discovery_archives_previous_version(isolated_repository):
    first = add_discovery(isolated_repository, result={"title": "Título antigo"})
    add_discovery(isolated_repository, result={"title": "Título atualizado"})

    history = isolated_repository.list_discovery_history(first["id"])
    current = isolated_repository.list_discovery_results()[0]

    assert len(history) == 1
    assert history[0]["previous_result"] == {"title": "Título antigo"}
    assert current["result"] == {"title": "Título atualizado"}


def test_enqueue_extraction_is_idempotent(isolated_repository):
    discovery = add_discovery(isolated_repository)

    first = isolated_repository.enqueue_extraction(discovery["id"])
    second = isolated_repository.enqueue_extraction(discovery["id"])

    assert first["id"] == second["id"]
    assert second["status"] == "pending"
    assert second["attempts"] == 0
    assert len(isolated_repository.list_extraction_queue()) == 1


def test_enqueue_extraction_requires_existing_discovery(isolated_repository):
    with pytest.raises(ValueError, match="não encontrado"):
        isolated_repository.enqueue_extraction(999)


def test_enqueue_pending_extractions_adds_only_missing_items(isolated_repository):
    first = add_discovery(isolated_repository, url="https://empresa.example/vagas/1")
    add_discovery(isolated_repository, url="https://empresa.example/vagas/2")
    isolated_repository.enqueue_extraction(first["id"])

    added = isolated_repository.enqueue_pending_extractions(limit=10)

    assert added == 1
    assert len(isolated_repository.list_extraction_queue()) == 2
    assert isolated_repository.enqueue_pending_extractions(limit=10) == 0


def test_claim_next_extraction_returns_lease_and_discovery(isolated_repository):
    discovery = add_discovery(isolated_repository)
    queue = isolated_repository.enqueue_extraction(discovery["id"])

    claimed = isolated_repository.claim_next_extraction(lease_seconds=60)

    assert claimed["id"] == queue["id"]
    assert isolated_repository.list_extraction_queue()[0]["status"] == "running"
    assert claimed["attempts"] == 1
    assert claimed["lease_token"]
    assert claimed["discovery_result"]["id"] == discovery["id"]
    assert claimed["discovery_result"]["result"]["title"] == "Vaga encontrada"


def test_claim_does_not_return_same_running_task_twice(isolated_repository):
    discovery = add_discovery(isolated_repository)
    isolated_repository.enqueue_extraction(discovery["id"])

    first = isolated_repository.claim_next_extraction()
    second = isolated_repository.claim_next_extraction()

    assert first is not None
    assert second is None


def test_heartbeat_requires_current_lease_token(isolated_repository):
    discovery = add_discovery(isolated_repository)
    isolated_repository.enqueue_extraction(discovery["id"])
    claimed = isolated_repository.claim_next_extraction(lease_seconds=300)

    assert isolated_repository.heartbeat_extraction(
        claimed["id"], "token-incorreto", lease_seconds=120
    ) is None
    renewed = isolated_repository.heartbeat_extraction(
        claimed["id"], claimed["lease_token"], lease_seconds=600
    )

    assert renewed["id"] == claimed["id"]
    assert renewed["lease_expires_at"] > claimed["lease_expires_at"]


def test_completed_extraction_clears_lease(isolated_repository):
    discovery = add_discovery(isolated_repository)
    isolated_repository.enqueue_extraction(discovery["id"])
    claimed = isolated_repository.claim_next_extraction()

    completed = isolated_repository.mark_extraction_completed(
        claimed["id"], claimed["lease_token"]
    )

    assert completed["status"] == "completed"
    listed = isolated_repository.list_extraction_queue(status="completed")[0]
    assert listed["completed_at"]
    assert "lease_token" not in listed
    assert isolated_repository.claim_next_extraction() is None


def test_old_worker_cannot_complete_after_lease_recovery(isolated_repository):
    discovery = add_discovery(isolated_repository)
    isolated_repository.enqueue_extraction(discovery["id"])
    old_claim = isolated_repository.claim_next_extraction()

    with repo.db_session(immediate=True) as conn:
        repo.execute(
            conn,
            "UPDATE extraction_queue SET lease_expires_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(), old_claim["id"]),
        )

    assert isolated_repository.recover_interrupted_extractions() == 1
    assert isolated_repository.mark_extraction_completed(
        old_claim["id"], old_claim["lease_token"]
    ) is None

    new_claim = isolated_repository.claim_next_extraction()
    assert new_claim["id"] == old_claim["id"]
    assert new_claim["lease_token"] != old_claim["lease_token"]
    assert new_claim["attempts"] == 2


def test_failed_extraction_is_requeued_before_max_attempts(isolated_repository):
    discovery = add_discovery(isolated_repository)
    isolated_repository.enqueue_extraction(discovery["id"], max_attempts=2)
    claimed = isolated_repository.claim_next_extraction()

    failed = isolated_repository.mark_extraction_failed(
        claimed["id"], claimed["lease_token"], "Falha temporária", retry_delay_seconds=1
    )

    assert failed["status"] == "pending"
    assert failed["last_error"] == "Falha temporária"
    assert isolated_repository.list_extraction_queue()[0]["lease_expires_at"] is None


def test_failed_extraction_stops_after_max_attempts(isolated_repository):
    discovery = add_discovery(isolated_repository)
    isolated_repository.enqueue_extraction(discovery["id"], max_attempts=1)
    claimed = isolated_repository.claim_next_extraction()

    failed = isolated_repository.mark_extraction_failed(
        claimed["id"], claimed["lease_token"], "Erro definitivo", retry_delay_seconds=1
    )

    assert failed["status"] == "failed"
    assert isolated_repository.claim_next_extraction() is None


def test_extraction_queue_listing_never_exposes_lease_token(isolated_repository):
    discovery = add_discovery(isolated_repository)
    isolated_repository.enqueue_extraction(discovery["id"])
    isolated_repository.claim_next_extraction()

    row = isolated_repository.list_extraction_queue()[0]

    assert row["status"] == "running"
    assert "lease_token" not in row


def test_incomplete_extraction_is_saved_without_candidate_or_score(isolated_repository):
    saved = isolated_repository.save_extracted_job(make_extracted())
    fetched = isolated_repository.get_extracted_job(BASE_URL)

    assert saved["status"] == "incomplete"
    assert fetched["extracted_job"]["company"] is None
    assert fetched["extracted_job"]["company_hint"] == "Empresa Exemplo"
    assert isolated_repository.list_candidates() == []


def test_extraction_can_be_enriched_from_incomplete_to_complete(isolated_repository):
    first = isolated_repository.save_extracted_job(make_extracted())
    second = isolated_repository.save_extracted_job(
        make_extracted(company="Empresa Exemplo", missing_fields=[])
    )

    fetched = isolated_repository.get_extracted_job(BASE_URL)

    assert first["id"] == second["id"]
    assert second["result"] == "updated"
    assert fetched["status"] == "complete"
    assert fetched["extracted_job"]["company"] == "Empresa Exemplo"
    assert len(isolated_repository.list_extracted_jobs()) == 1


def test_incomplete_update_does_not_erase_existing_description(isolated_repository):
    long_description = "Descrição detalhada da vaga. " * 20
    isolated_repository.save_extracted_job(make_extracted(description=long_description))
    isolated_repository.save_extracted_job(make_extracted(description=None))

    fetched = isolated_repository.get_extracted_job(BASE_URL)

    assert fetched["extracted_job"]["description"] == long_description.strip()


def test_extraction_merge_keeps_unique_application_urls_and_warnings(isolated_repository):
    isolated_repository.save_extracted_job(
        make_extracted(
            application_urls=["https://empresa.example/candidatar/1"],
            warnings=["Página parcial"],
        )
    )
    isolated_repository.save_extracted_job(
        make_extracted(
            application_urls=[
                "https://empresa.example/candidatar/1",
                "https://empresa.example/candidatar/2",
            ],
            warnings=["Página parcial", "Data ausente"],
        )
    )

    extracted = isolated_repository.get_extracted_job(BASE_URL)["extracted_job"]

    assert extracted["application_urls"] == [
        "https://empresa.example/candidatar/1",
        "https://empresa.example/candidatar/2",
    ]
    assert extracted["warnings"] == ["Página parcial", "Data ausente"]


def test_extraction_uses_canonical_url_for_deduplication(isolated_repository):
    first = make_extracted(canonical_url="https://empresa.example/vagas/canonica")
    second = make_extracted(
        page_url="https://empresa.example/vagas/espelho",
        canonical_url="https://empresa.example/vagas/canonica",
        company="Empresa Exemplo",
    )

    saved_first = isolated_repository.save_extracted_job(first)
    saved_second = isolated_repository.save_extracted_job(second)

    assert saved_first["id"] == saved_second["id"]
    assert len(isolated_repository.list_extracted_jobs()) == 1


def test_candidate_duplicate_enriches_payload_without_losing_existing_data(isolated_repository):
    first = make_candidate(description="Descrição inicial.", requirements=None)
    second = make_candidate(
        original_job_url=BASE_URL + "?utm_source=campanha",
        description="Descrição mais completa da oportunidade e das atividades.",
        requirements="Ensino médio completo e experiência administrativa.",
        application_urls=["https://empresa.example/candidatar/123"],
    )

    inserted = isolated_repository.save_candidate(first)
    duplicate = isolated_repository.save_candidate(second)
    stored = isolated_repository.list_candidates()[0]
    payload = stored

    assert duplicate["status"] == "duplicate"
    assert duplicate["id"] == inserted["id"]
    assert payload["description"] == second.description
    assert payload["requirements"] == second.requirements
    assert payload["application_url"] == "https://empresa.example/candidatar/123"


def test_candidate_duplicate_preserves_existing_fields_when_new_payload_is_empty(isolated_repository):
    first = make_candidate(
        description="Descrição que não pode ser apagada.",
        requirements="Requisito que não pode ser apagado.",
    )
    second = make_candidate(description=None, requirements=None)

    isolated_repository.save_candidate(first)
    isolated_repository.save_candidate(second)
    payload = isolated_repository.list_candidates()[0]

    assert payload["description"] == first.description
    assert payload["requirements"] == first.requirements


def test_candidate_duplicate_merges_evidence_and_recalculates_score(isolated_repository):
    first = make_candidate()
    second = make_candidate(original_job_url=BASE_URL + "?utm_campaign=teste")

    isolated_repository.save_candidate(first, verified_company_evidence())
    result = isolated_repository.save_candidate(
        second,
        CandidateEvidence(
            original_page_verified=True,
            evidence_records=[EvidenceRecord(
                criterion="original_page_verified",
                evidence_url=BASE_URL,
                method="manual_review",
                checked_at=datetime.now(timezone.utc),
                result="verified",
            )],
        ),
    )

    stored = isolated_repository.list_candidates()[0]
    evidence = stored["evidence"]

    assert result["status"] == "duplicate"
    assert result["score"] == 50
    assert evidence["company_verified"] is True
    assert evidence["original_page_verified"] is True
    assert len(evidence["evidence_records"]) == 2
