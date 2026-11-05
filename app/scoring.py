from app.models import CandidateIn


def score_candidate(candidate: CandidateIn) -> tuple[int, str, list[str]]:
    """Pontua apenas evidências recebidas; não presume que uma URL é válida."""
    score = 0
    reasons: list[str] = []

    if candidate.company_verified:
        score += 25
        reasons.append("Empresa identificada e verificada (+25)")
    else:
        reasons.append("Empresa ainda não verificada")

    if candidate.original_page_verified:
        score += 25
        reasons.append("Página original da vaga confirmada (+25)")
    else:
        reasons.append("Página original da vaga não confirmada")

    if candidate.application_link_verified and candidate.application_url:
        score += 20
        reasons.append("Link de candidatura verificado (+20)")
    else:
        reasons.append("Link de candidatura não verificado")

    if candidate.job_content_verified:
        score += 15
        reasons.append("Conteúdo da oportunidade consistente (+15)")
    else:
        reasons.append("Conteúdo da oportunidade ainda não verificado")

    if candidate.date_verified:
        score += 15
        reasons.append("Data/validade verificadas (+15)")
    else:
        reasons.append("Data/validade ainda não verificadas")

    # Travas obrigatórias: pontuação alta nunca substitui evidências essenciais.
    mandatory_ok = (
        candidate.company_verified
        and candidate.original_page_verified
        and candidate.application_link_verified
        and candidate.application_url is not None
        and candidate.job_content_verified
    )

    if not mandatory_ok:
        status = "review" if score >= 60 else "rejected"
        reasons.append("Não aprovada: falta pelo menos uma verificação obrigatória")
    elif score >= 85:
        status = "approved"
    elif score >= 60:
        status = "review"
    else:
        status = "rejected"

    return score, status, reasons
