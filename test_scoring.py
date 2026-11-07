
import pytest

from app.models import CandidateEvidence
from app.scoring import score_candidate


def test_all_evidence_approves_candidate():
    evidence = CandidateEvidence(
        company_verified=True,
        original_page_verified=True,
        application_link_verified=True,
        job_content_verified=True,
        date_verified=True,
    )

    score, status, reasons = score_candidate(evidence)

    assert score == 100
    assert status == "approved"
    assert len(reasons) == 6


@pytest.mark.parametrize(
    "missing_field",
    [
        "company_verified",
        "original_page_verified",
        "application_link_verified",
        "job_content_verified",
        "date_verified",
    ],
)
def test_missing_evidence_requires_review(missing_field):
    data = {
        "company_verified": True,
        "original_page_verified": True,
        "application_link_verified": True,
        "job_content_verified": True,
        "date_verified": True,
    }

    data[missing_field] = False

    evidence = CandidateEvidence(**data)

    score, status, reasons = score_candidate(evidence)

    assert score < 100
    assert status == "review"
    assert any("Em revisão" in reason for reason in reasons)


def test_no_evidence_requires_review():
    evidence = CandidateEvidence()

    score, status, reasons = score_candidate(evidence)

    assert score == 0
    assert status == "review"


def test_rejected_candidate_never_gets_approved():
    evidence = CandidateEvidence(
        company_verified=True,
        original_page_verified=True,
        application_link_verified=True,
        job_content_verified=True,
        date_verified=True,
        rejected_reason="Empresa identificada como fraudulenta.",
    )

    score, status, reasons = score_candidate(evidence)

    assert score == 100
    assert status == "rejected"
    assert any("fraudulenta" in reason for reason in reasons)
