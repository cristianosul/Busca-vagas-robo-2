
from datetime import datetime, timezone

from app.models import CandidateIn, CandidateVerification


def score_candidate(
    candidate: CandidateIn,
    verification: CandidateVerification | None = None,
) -> tuple[int, str, list[str]]:

    verification = verification or CandidateVerification()

    score = 0
    reasons: list[str] = []

    checks = [
        (
            verification.company_verified,
            25,
            "Empresa identificada e verificada",
            "Empresa ainda não verificada",
        ),
        (
            verification.original_page_verified,
            25,
            "Página original da vaga confirmada",
            "Página original da vaga não confirmada",
        ),
        (
            verification.application_link_verified
            and candidate.application_url is not None,
            20,
            "Link de candidatura verificado",
            "Link de candidatura não verificado",
        ),
        (
            verification.job_content_verified,
            15,
            "Conteúdo da oportunidade consistente",
            "Conteúdo da oportunidade ainda não verificado",
        ),
        (
            verification.date_verified,
            15,
            "Data e validade verificadas",
            "Data e validade ainda não verificadas",
        ),
    ]

    for verified, points, success, failure in checks:
        if verified:
            score += points
            reasons.append(f"{success} (+{points})")
        else:
            reasons.append(failure)

    now = datetime.now(timezone.utc)
    expired = False

    if candidate.valid_through is not None:
        expiry = candidate.valid_through

        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        expired = expiry < now

    if expired:
        reasons.append("Vaga vencida: validade já ultrapassada")
        return score, "rejected", reasons

    mandatory_ok = (
        verification.company_verified
        and verification.original_page_verified
        and verification.application_link_verified
        and candidate.application_url is not None
        and verification.job_content_verified
        and verification.date_verified
        and candidate.valid_through is not None
    )

    if not mandatory_ok:
        status = "review" if score >= 60 else "rejected"
        reasons.append(
            "Não aprovada: faltam verificações obrigatórias"
        )
    elif score >= 85:
        status = "approved"
    elif score >= 60:
        status = "review"
    else:
        status = "rejected"

    return score, status, reasons
