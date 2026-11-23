"""Orquestra as cinco verificações obrigatórias de uma oportunidade.

Este módulo não faz requisições de rede nem persiste candidatos. As verificações
concretas são fornecidas por funções internas (checkers), que devem retornar
uma evidência rastreável. Sem um checker, o critério permanece inconclusivo.
Assim, dados extraídos ou URLs acessíveis não são tratados como prova por si só.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping
from urllib.parse import urlparse

from pydantic import HttpUrl

from app.models import (
    CandidateEvidence,
    EvidenceCriterion,
    EvidenceRecord,
    EvidenceResult,
    ExtractedJobData,
)
from app.scoring import score_candidate


@dataclass(frozen=True)
class VerificationCheck:
    """Resultado de um checker interno para um único critério."""

    result: EvidenceResult
    evidence_url: str
    method: str
    details: str | None = None


Checker = Callable[[ExtractedJobData], VerificationCheck]

CRITERIA: tuple[EvidenceCriterion, ...] = (
    "company_verified",
    "original_page_verified",
    "application_link_verified",
    "job_content_verified",
    "date_verified",
)

CRITERION_FLAGS: Mapping[EvidenceCriterion, str] = {
    "company_verified": "company_verified",
    "original_page_verified": "original_page_verified",
    "application_link_verified": "application_link_verified",
    "job_content_verified": "job_content_verified",
    "date_verified": "date_verified",
}


class CandidateVerificationError(ValueError):
    """Indica um resultado de checker inválido ou sem procedência rastreável."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_check(
    criterion: EvidenceCriterion,
    check: VerificationCheck,
) -> tuple[EvidenceResult, HttpUrl, str, str | None]:
    if not isinstance(check, VerificationCheck):
        raise CandidateVerificationError(
            f"O checker de '{criterion}' não retornou VerificationCheck."
        )

    if check.result not in {"verified", "not_verified", "inconclusive"}:
        raise CandidateVerificationError(
            f"Resultado inválido para o critério '{criterion}'."
        )

    if not isinstance(check.method, str) or len(check.method.strip()) < 2:
        raise CandidateVerificationError(
            f"O método de verificação de '{criterion}' não foi informado."
        )

    try:
        evidence_url = HttpUrl(check.evidence_url)
    except (TypeError, ValueError) as exc:
        raise CandidateVerificationError(
            f"URL de evidência inválida para '{criterion}'."
        ) from exc

    parsed = urlparse(str(evidence_url))
    if parsed.scheme != "https" or not parsed.hostname:
        raise CandidateVerificationError(
            f"A evidência de '{criterion}' precisa usar uma URL HTTPS válida."
        )

    details = check.details
    if details is not None:
        if not isinstance(details, str):
            raise CandidateVerificationError(
                f"Detalhes inválidos para o critério '{criterion}'."
            )
        details = details.strip()[:1000] or None

    return check.result, evidence_url, check.method.strip()[:100], details


def verify_extracted_job(
    extracted_job: ExtractedJobData | Mapping,
    *,
    checkers: Mapping[EvidenceCriterion, Checker] | None = None,
    checked_at: datetime | None = None,
) -> tuple[CandidateEvidence, int, str, list[str]]:
    """Executa os checkers disponíveis e calcula a situação da oportunidade.

    Cada critério sem checker recebe resultado ``inconclusive``. Checkers não
    podem ser substituídos por flags fornecidas no conteúdo extraído. Uma
    confirmação só é registrada quando o checker retorna ``verified`` com
    método e URL HTTPS de evidência.

    O retorno contém (evidências, pontuação, status, motivos). Esta função é
    pura em relação ao banco: não cria, atualiza ou publica candidatos.
    """
    if isinstance(extracted_job, ExtractedJobData):
        job = extracted_job
    else:
        job = ExtractedJobData.model_validate(extracted_job)

    if checkers is None:
        checkers = {}
    if not isinstance(checkers, Mapping):
        raise TypeError("checkers deve ser um mapeamento de critérios para funções.")

    unknown = set(checkers) - set(CRITERIA)
    if unknown:
        raise CandidateVerificationError(
            "Critérios de verificação desconhecidos: " + ", ".join(sorted(unknown))
        )

    timestamp = checked_at or _utc_now()
    if not isinstance(timestamp, datetime):
        raise TypeError("checked_at deve ser um datetime.")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    records: list[EvidenceRecord] = []
    flags: dict[str, bool] = {flag: False for flag in CRITERION_FLAGS.values()}

    for criterion in CRITERIA:
        checker = checkers.get(criterion)
        if checker is None:
            result: EvidenceResult = "inconclusive"
            evidence_url = job.page_url
            method = "no_checker_configured"
            details = "Verificação ainda não configurada; nenhuma comprovação foi presumida."
        else:
            if not callable(checker):
                raise CandidateVerificationError(
                    f"O checker de '{criterion}' não é executável."
                )
            check = checker(job)
            result, evidence_url, method, details = _validate_check(
                criterion,
                check,
            )

        records.append(
            EvidenceRecord(
                criterion=criterion,
                evidence_url=evidence_url,
                method=method,
                checked_at=timestamp,
                result=result,
                details=details,
            )
        )
        flags[CRITERION_FLAGS[criterion]] = result == "verified"

    evidence = CandidateEvidence(
        **flags,
        evidence_records=records,
    )
    score, status, reasons = score_candidate(evidence)
    return evidence, score, status, reasons


__all__ = [
    "CRITERIA",
    "CRITERION_FLAGS",
    "CandidateVerificationError",
    "VerificationCheck",
    "verify_extracted_job",
]
