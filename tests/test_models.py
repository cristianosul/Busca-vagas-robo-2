from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.models import (
    CandidateEvidence,
    CandidateIn,
    EvidenceRecord,
    ExtractedJobData,
)
from app.scoring import WEIGHTS, score_candidate


BASE_URL = "https://example.com/vagas/123"
EVIDENCE_URL = "https://example.com/evidencias/123"

CRITERIA = {
    "company_verified": 25,
    "original_page_verified": 25,
    "application_link_verified": 20,
    "job_content_verified": 15,
    "date_verified": 15,
}


def make_record(criterion, result="verified", checked_at=None):
    return EvidenceRecord(
        criterion=criterion,
        evidence_url=EVIDENCE_URL,
        method="manual_review",
        checked_at=checked_at or datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
        result=result,
    )


def complete_evidence():
    return CandidateEvidence(
        **{criterion: True for criterion in CRITERIA},
        evidence_records=[make_record(criterion) for criterion in CRITERIA],
    )


def test_extracted_job_allows_unknown_company_without_becoming_candidate():
    extracted = ExtractedJobData(
        title="Auxiliar administrativo",
        company=None,
        company_hint="Empresa mencionada no anúncio",
        page_url=BASE_URL,
        extraction_method="json_ld",
    )

    assert extracted.company is None
    assert extracted.company_hint == "Empresa mencionada no anúncio"
    assert extracted.title == "Auxiliar administrativo"


def test_extracted_job_preserves_full_description_and_requirements():
    description = "Descrição completa da oportunidade. " * 100
    requirements = "Requisitos detalhados da vaga. " * 100

    extracted = ExtractedJobData(
        title="Analista de sistemas",
        company="Empresa Exemplo",
        description=description,
        requirements=requirements,
        page_url=BASE_URL,
        extraction_method="html_fallback",
    )

    assert extracted.description == description.strip()
    assert extracted.requirements == requirements.strip()


def test_extracted_job_deduplicates_application_urls_and_keeps_order():
    extracted = ExtractedJobData(
        title="Desenvolvedor Python",
        page_url=BASE_URL,
        extraction_method="json_ld",
        application_urls=[
            "https://example.com/candidatar/123",
            "https://example.com/candidatar/456",
            "https://example.com/candidatar/123",
        ],
    )

    assert [str(url) for url in extracted.application_urls] == [
        "https://example.com/candidatar/123",
        "https://example.com/candidatar/456",
    ]


def test_extracted_job_keeps_extraction_metadata_and_warnings():
    extracted = ExtractedJobData(
        title="Assistente financeiro",
        page_url=BASE_URL,
        extraction_method="json_ld",
        structured_data_found=True,
        missing_fields=["company", "date_posted"],
        warnings=["Data de publicação não encontrada"],
        canonical_url="https://example.com/vagas/123-oficial",
        identifier="JOB-123",
        job_url="https://example.com/vagas/123",
    )

    assert extracted.structured_data_found is True
    assert extracted.missing_fields == ["company", "date_posted"]
    assert extracted.warnings == ["Data de publicação não encontrada"]
    assert str(extracted.canonical_url) == "https://example.com/vagas/123-oficial"
    assert extracted.identifier == "JOB-123"


def test_candidate_still_requires_company_until_database_schema_changes():
    with pytest.raises(ValidationError):
        CandidateIn(
            title="Auxiliar administrativo",
            source_name="Fonte Exemplo",
            source_url=BASE_URL,
            original_job_url=BASE_URL,
        )


def test_candidate_accepts_description_requirements_and_multiple_application_links():
    candidate = CandidateIn(
        title="Auxiliar administrativo",
        company="Empresa Exemplo",
        description="Atividades completas da vaga.",
        requirements="Ensino médio completo.",
        source_name="Fonte Exemplo",
        source_url="https://example.com/fonte",
        original_job_url=BASE_URL,
        application_urls=[
            "https://example.com/candidatar/123",
            "https://example.com/candidatar/456",
        ],
        identifier="JOB-123",
        extraction_method="json_ld",
        structured_data_found=True,
    )

    assert candidate.description == "Atividades completas da vaga."
    assert candidate.requirements == "Ensino médio completo."
    assert len(candidate.application_urls) == 2
    assert str(candidate.application_url) == "https://example.com/candidatar/123"
    assert candidate.identifier == "JOB-123"
    assert candidate.structured_data_found is True


def test_candidate_preserves_legacy_single_application_url_field():
    candidate = CandidateIn(
        title="Analista de dados",
        company="Empresa Exemplo",
        source_name="Fonte Exemplo",
        source_url="https://example.com/fonte",
        original_job_url=BASE_URL,
        application_url="https://example.com/candidatar/123",
    )

    assert str(candidate.application_url) == "https://example.com/candidatar/123"
    assert [str(url) for url in candidate.application_urls] == [
        "https://example.com/candidatar/123"
    ]


def test_scoring_weights_remain_approved_values_and_total_100():
    assert WEIGHTS == CRITERIA
    assert sum(WEIGHTS.values()) == 100


def test_all_five_verified_criteria_score_100_and_approve():
    score, status, _ = score_candidate(complete_evidence())

    assert score == 100
    assert status == "approved"


@pytest.mark.parametrize(
    ("criterion", "expected_score"),
    [
        ("company_verified", 75),
        ("original_page_verified", 75),
        ("application_link_verified", 80),
        ("job_content_verified", 85),
        ("date_verified", 85),
    ],
)
def test_each_missing_verification_keeps_candidate_in_review(criterion, expected_score):
    values = {name: True for name in CRITERIA}
    values[criterion] = False
    evidence = CandidateEvidence(
        **values,
        evidence_records=[make_record(name) for name in CRITERIA],
    )

    score, status, _ = score_candidate(evidence)

    assert score == expected_score
    assert status == "review"


@pytest.mark.parametrize("new_result", ["not_verified", "inconclusive"])
def test_newer_negative_or_inconclusive_evidence_invalidates_older_positive(new_result):
    older = make_record(
        "company_verified",
        "verified",
        datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc),
    )
    newer = make_record(
        "company_verified",
        new_result,
        datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(ValidationError, match="evidência mais recente"):
        CandidateEvidence(
            company_verified=True,
            evidence_records=[older, newer],
        )


def test_newer_positive_evidence_allows_true_even_if_older_record_was_negative():
    older = make_record(
        "company_verified",
        "not_verified",
        datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc),
    )
    newer = make_record(
        "company_verified",
        "verified",
        datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
    )

    evidence = CandidateEvidence(
        company_verified=True,
        evidence_records=[older, newer],
    )

    assert evidence.company_verified is True


def test_same_time_positive_and_negative_records_do_not_allow_true():
    checked_at = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(ValidationError, match="evidência mais recente"):
        CandidateEvidence(
            company_verified=True,
            evidence_records=[
                make_record("company_verified", "verified", checked_at),
                make_record("company_verified", "inconclusive", checked_at),
            ],
        )


def test_naive_and_aware_datetimes_are_compared_as_utc():
    older = make_record(
        "company_verified",
        "verified",
        datetime(2026, 11, 20, 11, 0),
    )
    newer = make_record(
        "company_verified",
        "not_verified",
        datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(ValidationError, match="evidência mais recente"):
        CandidateEvidence(
            company_verified=True,
            evidence_records=[older, newer],
        )


def test_false_verification_flags_can_retain_non_verified_evidence_history():
    evidence = CandidateEvidence(
        company_verified=False,
        evidence_records=[make_record("company_verified", "inconclusive")],
    )

    assert evidence.company_verified is False
    score, status, _ = score_candidate(evidence)
    assert score == 0
    assert status == "review"


def test_legacy_candidate_fields_and_defaults_remain_compatible():
    candidate = CandidateIn(
        title="Assistente administrativo",
        company="Empresa Exemplo",
        source_name="Fonte Exemplo",
        source_url="https://example.com/fonte",
        original_job_url=BASE_URL,
        location="Caxias do Sul - RS",
        mode="Presencial",
        employment_type="Efetivo (CLT)",
        date_posted=datetime(2026, 11, 19, tzinfo=timezone.utc),
    )

    assert candidate.location == "Caxias do Sul - RS"
    assert candidate.mode == "Presencial"
    assert candidate.employment_type == "Efetivo (CLT)"
    assert candidate.date_posted == datetime(2026, 11, 19, tzinfo=timezone.utc)
    assert candidate.application_urls == []


def test_models_reject_unexpected_fields():
    with pytest.raises(ValidationError):
        ExtractedJobData(
            title="Vaga",
            page_url=BASE_URL,
            extraction_method="json_ld",
            approved=True,
        )
