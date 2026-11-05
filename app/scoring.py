from app.models import CandidateEvidence


WEIGHTS = {
    "company_verified": 25,
    "original_page_verified": 25,
    "application_link_verified": 20,
    "job_content_verified": 15,
    "date_verified": 15,
}


def score_candidate(evidence: CandidateEvidence) -> tuple[int, str, list[str]]:
    """Só aprova com todas as evidências obrigatórias; ausência de prova vira revisão."""
    score = 0
    reasons: list[str] = []

    labels = {
        "company_verified": "Empresa identificada e verificada",
        "original_page_verified": "Página original da vaga confirmada",
        "application_link_verified": "Link de candidatura verificado",
        "job_content_verified": "Conteúdo da oportunidade consistente",
        "date_verified": "Data e validade verificadas",
    }

    for field, weight in WEIGHTS.items():
        if getattr(evidence, field):
            score += weight
            reasons.append(f"{labels[field]} (+{weight})")
        else:
            reasons.append(f"{labels[field]} ainda não verificada")

    if evidence.rejected_reason:
        return score, "rejected", reasons + [f"Rejeitada: {evidence.rejected_reason}"]

    mandatory_ok = all(getattr(evidence, field) for field in WEIGHTS)
    if mandatory_ok and score >= 85:
        return score, "approved", reasons + ["Aprovada: todas as verificações obrigatórias foram concluídas"]

    return score, "review", reasons + ["Em revisão: faltam evidências obrigatórias; não publicar"]
