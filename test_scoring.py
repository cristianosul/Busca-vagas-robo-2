
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models import CandidateEvidence, EvidenceRecord
from app.scoring import score_candidate


ALL_EVIDENCE = {
    "company_verified": True,
    "original_page_verified": True,
    "application_link_verified": True,
    "job_content_verified": True,
    "date_verified": True,
}


def make_evidence_records(result="verified"):
    return [
        EvidenceRecord(
            criterion=criterion,
            evidence_url="https://example.com/evidence",
            method="manual_review",
            checked_at=datetime.now(timezone.utc),
            result=result,
        )
        for criterion in ALL_EVIDENCE
    ]


def make_complete_evidence(**overrides):
    data = {
        **ALL_EVIDENCE,
        "evidence_records": make_evidence_records(),
    }
    data.update(overrides)
    return CandidateEvidence(**data)


def test_all_evidence_approves_candidate():
    evidence = make_complete_evidence()

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
    data = {
        **ALL_EVIDENCE,
        "evidence_records": make_evidence_records(),
    }
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
    evidence = make_complete_evidence(
        rejected_reason="Empresa identificada como fraudulenta."
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
    evidence = make_complete_evidence(
        rejected_reason="Vaga identificada como fraudulenta."
    )

    score, status, reasons = score_candidate(evidence)

    assert score == 100
    assert status == "rejected"
    assert any("fraudulenta" in reason for reason in reasons)


def test_all_incomplete_evidence_combinations_require_review():
    fields = list(ALL_EVIDENCE)

    for mask in range(1, 2 ** len(fields)):
        data = {
            field: bool(mask & (1 << index))
            for index, field in enumerate(fields)
        }

        if all(data.values()):
            continue

        data["evidence_records"] = make_evidence_records()

        evidence = CandidateEvidence(**data)

        score, status, reasons = score_candidate(evidence)

        assert status == "review"
        assert score < 100
        assert any("Em revisão" in reason for reason in reasons)


def test_maximum_score_is_exactly_100():
    evidence = make_complete_evidence()

    score, status, reasons = score_candidate(evidence)

    assert score == sum([25, 25, 20, 15, 15])
    assert score == 100
    assert status == "approved"


def test_verified_boolean_without_matching_record_is_rejected():
    with pytest.raises(ValidationError):
        CandidateEvidence(
            company_verified=True,
        )


@pytest.mark.parametrize(
    "result",
    ["not_verified", "inconclusive"],
)
def test_non_verified_record_cannot_validate_true_boolean(result):
    record = EvidenceRecord(
        criterion="company_verified",
        evidence_url="https://example.com/evidence",
        method="manual_review",
        checked_at=datetime.now(timezone.utc),
        result=result,
    )

    with pytest.raises(ValidationError):
        CandidateEvidence(
            company_verified=True,
            evidence_records=[record],
        )


def test_matching_verified_record_allows_true_boolean():
    record = EvidenceRecord(
        criterion="company_verified",
        evidence_url="https://example.com/evidence",
        method="manual_review",
        checked_at=datetime.now(timezone.utc),
        result="verified",
    )

    evidence = CandidateEvidence(
        company_verified=True,
        evidence_records=[record],
    )

    assert evidence.company_verified is True
    
