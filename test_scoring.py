
import pytest

from app.models import CandidateEvidence
from app.scoring import score_candidate


ALL_EVIDENCE = {
    "company_verified": True,
    "original_page_verified": True,
    "application_link_verified": True,
    "job_content_verified": True,
    "date_verified": True,
}


def test_all_evidence_approves_candidate():
    evidence = CandidateEvidence(**ALL_EVIDENCE)

    score, status, reasons = score_candidate(evidence)

    assert score == 100
    assert status == "approved"
    assert len(reasons) == 6


@pytest.mark.parametrize(
    ("missing_field", "expected_score"),
    [
        ("company_verified", 75),
        ("original_page_verified", 75),
        ("application_link_verified", 80),
        ("job_content_verified", 85),
        ("date_verified", 85),
    ],
)
def test_missing_evidence_requires_review(
    missing_field,
    expected_score,
):
    data = ALL_EVIDENCE.copy()
    data[missing_field] = False

    evidence = CandidateEvidence(**data)

    score, status, reasons = score_candidate(evidence)

    assert score == expected_score
    assert status == "review"
    assert any("Em revisão" in reason for reason in reasons)


def test_no_evidence_requires_review():
    evidence = CandidateEvidence()

    score, status, reasons = score_candidate(evidence)

    assert score == 0
    assert status == "review"


def test_rejected_candidate_never_gets_approved():
    evidence = CandidateEvidence(
        **ALL_EVIDENCE,
        rejected_reason="Empresa identificada como fraudulenta.",
    )

    score, status, reasons = score_candidate(evidence)

    assert score == 100
    assert status == "rejected"
    assert any("fraudulenta" in reason for reason in reasons)


def test_rejected_candidate_without_evidence_remains_rejected():
    evidence = CandidateEvidence(
        rejected_reason="Fonte identificada como não confiável.",
    )

    score, status, reasons = score_candidate(evidence)

    assert score == 0
    assert status == "rejected"
    assert any("não confiável" in reason for reason in reasons)


def test_rejection_has_priority_over_complete_evidence():
    evidence = CandidateEvidence(
        **ALL_EVIDENCE,
        rejected_reason="Vaga identificada como fraudulenta.",
    )

    score, status, reasons = score_candidate(evidence)

    assert score == 100
    assert status != "approved"
    assert status == "rejected"
    assert any("fraudulenta" in reason for reason in reasons)
    
