from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.candidate_verifier import (
    CRITERIA,
    CandidateVerificationError,
    VerificationCheck,
    verify_extracted_job,
)
from app.models import ExtractedJobData


PAGE_URL = "https://empresa.example/vagas/123"
EVIDENCE_URL = "https://empresa.example/evidencias/123"
CHECKED_AT = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
WEIGHTS = {
    "company_verified": 25,
    "original_page_verified": 25,
    "application_link_verified": 20,
    "job_content_verified": 15,
    "date_verified": 15,
}


def make_job(**overrides):
    values = {
        "title": "Auxiliar administrativo",
        "company": "Empresa Exemplo",
        "description": "Atividades administrativas e atendimento.",
        "location": "Caxias do Sul - RS",
        "page_url": PAGE_URL,
        "extraction_method": "json_ld",
    }
    values.update(overrides)
    return ExtractedJobData(**values)


def verified_check(**overrides):
    values = {
        "result": "verified",
        "evidence_url": EVIDENCE_URL,
        "method": "manual_review",
        "details": "Evidência conferida.",
    }
    values.update(overrides)
    return VerificationCheck(**values)


def all_verified_checkers():
    return {
        criterion: (lambda _job: verified_check())
        for criterion in CRITERIA
    }


def test_without_checkers_keeps_all_criteria_inconclusive_and_candidate_in_review():
    evidence, score, status, reasons = verify_extracted_job(
        make_job(), checked_at=CHECKED_AT
    )

    assert score == 0
    assert status == "review"
    assert all(not getattr(evidence, criterion) for criterion in CRITERIA)
    assert len(evidence.evidence_records) == 5
    assert {record.result for record in evidence.evidence_records} == {"inconclusive"}
    assert {record.method for record in evidence.evidence_records} == {
        "no_checker_configured"
    }
    assert all(record.checked_at == CHECKED_AT for record in evidence.evidence_records)
    assert any("não publicar" in reason for reason in reasons)


def test_all_five_verified_checkers_score_100_and_approve():
    evidence, score, status, _ = verify_extracted_job(
        make_job(), checkers=all_verified_checkers(), checked_at=CHECKED_AT
    )

    assert score == 100
    assert status == "approved"
    assert all(getattr(evidence, criterion) for criterion in CRITERIA)
    assert len(evidence.evidence_records) == 5
    assert all(record.result == "verified" for record in evidence.evidence_records)
    assert all(str(record.evidence_url) == EVIDENCE_URL for record in evidence.evidence_records)


@pytest.mark.parametrize(
    ("missing_criterion", "expected_score"),
    [
        ("company_verified", 75),
        ("original_page_verified", 75),
        ("application_link_verified", 80),
        ("job_content_verified", 85),
        ("date_verified", 85),
    ],
)
def test_missing_checker_for_any_required_criterion_prevents_approval(
    missing_criterion, expected_score
):
    checkers = all_verified_checkers()
    del checkers[missing_criterion]

    evidence, score, status, _ = verify_extracted_job(make_job(), checkers=checkers)

    assert score == expected_score
    assert status == "review"
    assert getattr(evidence, missing_criterion) is False
    record = next(
        item for item in evidence.evidence_records
        if item.criterion == missing_criterion
    )
    assert record.result == "inconclusive"


@pytest.mark.parametrize("result", ["not_verified", "inconclusive"])
def test_non_positive_checker_result_does_not_set_verification_flag(result):
    checkers = all_verified_checkers()
    checkers["company_verified"] = lambda _job: verified_check(result=result)

    evidence, score, status, _ = verify_extracted_job(make_job(), checkers=checkers)

    assert evidence.company_verified is False
    assert score == 75
    assert status == "review"
    record = next(
        item for item in evidence.evidence_records
        if item.criterion == "company_verified"
    )
    assert record.result == result


def test_checker_receives_validated_extracted_job():
    received = []

    def checker(job):
        received.append(job)
        return verified_check()

    verify_extracted_job(make_job(), checkers={"company_verified": checker})

    assert len(received) == 1
    assert isinstance(received[0], ExtractedJobData)
    assert received[0].title == "Auxiliar administrativo"


def test_mapping_input_is_validated_and_supported():
    evidence, score, status, _ = verify_extracted_job(
        {
            "title": "Analista de sistemas",
            "page_url": PAGE_URL,
            "extraction_method": "html_fallback",
        }
    )

    assert len(evidence.evidence_records) == 5
    assert score == 0
    assert status == "review"


def test_invalid_extracted_job_mapping_is_rejected():
    with pytest.raises(ValidationError):
        verify_extracted_job({"page_url": "not-a-url", "extraction_method": "x"})


@pytest.mark.parametrize(
    "evidence_url",
    [
        "http://empresa.example/evidencia",
        "javascript:alert(1)",
        "not-a-url",
        "https://",
    ],
)
def test_checker_evidence_url_must_be_valid_https(evidence_url):
    checkers = {"company_verified": lambda _job: verified_check(evidence_url=evidence_url)}

    with pytest.raises(CandidateVerificationError, match="URL de evidência inválida|HTTPS"):
        verify_extracted_job(make_job(), checkers=checkers)


def test_checker_must_return_verification_check():
    with pytest.raises(CandidateVerificationError, match="não retornou VerificationCheck"):
        verify_extracted_job(make_job(), checkers={"company_verified": lambda _job: True})


def test_checker_must_be_callable():
    with pytest.raises(CandidateVerificationError, match="não é executável"):
        verify_extracted_job(make_job(), checkers={"company_verified": "not-callable"})


def test_unknown_criterion_is_rejected():
    with pytest.raises(CandidateVerificationError, match="Critérios de verificação desconhecidos"):
        verify_extracted_job(make_job(), checkers={"unknown": lambda _job: verified_check()})


def test_checkers_argument_must_be_a_mapping():
    with pytest.raises(TypeError, match="checkers deve ser um mapeamento"):
        verify_extracted_job(make_job(), checkers=[lambda _job: verified_check()])


def test_checker_method_must_have_at_least_two_non_whitespace_characters():
    checkers = {"company_verified": lambda _job: verified_check(method=" ")}

    with pytest.raises(CandidateVerificationError, match="método de verificação"):
        verify_extracted_job(make_job(), checkers=checkers)


def test_checker_details_are_trimmed_and_limited_to_1000_characters():
    details = "  " + ("x" * 1100) + "  "
    checkers = {"company_verified": lambda _job: verified_check(details=details)}

    evidence, _, _, _ = verify_extracted_job(make_job(), checkers=checkers)
    record = next(item for item in evidence.evidence_records if item.criterion == "company_verified")

    assert record.details == "x" * 1000


def test_empty_checker_details_are_normalized_to_none():
    checkers = {"company_verified": lambda _job: verified_check(details="   ")}

    evidence, _, _, _ = verify_extracted_job(make_job(), checkers=checkers)
    record = next(item for item in evidence.evidence_records if item.criterion == "company_verified")

    assert record.details is None


def test_naive_checked_at_is_normalized_to_utc():
    naive = datetime(2026, 11, 20, 12, 0)

    evidence, _, _, _ = verify_extracted_job(make_job(), checked_at=naive)

    assert all(record.checked_at.tzinfo is not None for record in evidence.evidence_records)
    assert all(record.checked_at.utcoffset().total_seconds() == 0 for record in evidence.evidence_records)


def test_non_datetime_checked_at_is_rejected():
    with pytest.raises(TypeError, match="checked_at deve ser um datetime"):
        verify_extracted_job(make_job(), checked_at="2026-11-20T12:00:00Z")


def test_checker_exception_is_not_silently_converted_to_positive_evidence():
    def broken_checker(_job):
        raise RuntimeError("falha simulada")

    with pytest.raises(RuntimeError, match="falha simulada"):
        verify_extracted_job(make_job(), checkers={"company_verified": broken_checker})
