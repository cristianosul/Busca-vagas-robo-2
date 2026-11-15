
import os
import uuid
from datetime import datetime, timezone

from app.models import (
    CandidateEvidence,
    CandidateIn,
    EvidenceRecord,
)
from app.repository import (
    DATABASE_URL,
    connect,
    execute,
    init_db,
    list_candidates,
    save_candidate,
)


TOKEN = uuid.uuid4().hex
CREATED_IDS = []

BASE_URL = "https://example.com"


def make_candidate(suffix: str) -> CandidateIn:
    """Cria uma oportunidade fictícia com URL exclusiva."""
    return CandidateIn(
        title=f"Vaga fictícia de teste {suffix}",
        company="Empresa Fictícia de Testes",
        location="Brasil",
        mode="Remoto",
        employment_type="Efetivo (CLT)",
        source_name="Fonte fictícia de testes",
        source_url=f"{BASE_URL}/fonte/{TOKEN}/{suffix}",
        original_job_url=f"{BASE_URL}/vaga/{TOKEN}/{suffix}",
        application_url=f"{BASE_URL}/candidatura/{TOKEN}/{suffix}",
        date_posted=datetime.now(timezone.utc),
    )


def make_record(criterion: str, result: str = "verified"):
    """Cria um registro fictício de evidência."""
    return EvidenceRecord(
        criterion=criterion,
        evidence_url=f"{BASE_URL}/evidencias/{TOKEN}/{criterion}",
        method="teste automatizado",
        checked_at=datetime.now(timezone.utc),
        result=result,
        details="Evidência fictícia usada exclusivamente no teste.",
    )


def make_partial_evidence() -> CandidateEvidence:
    """Quatro critérios verificados e um inconclusivo."""
    criteria = [
        "company_verified",
        "original_page_verified",
        "application_link_verified",
        "job_content_verified",
    ]

    records = [
        make_record(criterion)
        for criterion in criteria
    ]

    records.append(
        make_record("date_verified", "inconclusive")
    )

    return CandidateEvidence(
        company_verified=True,
        original_page_verified=True,
        application_link_verified=True,
        job_content_verified=True,
        date_verified=False,
        evidence_records=records,
    )


def make_complete_evidence() -> CandidateEvidence:
    """Cria evidências verificadas para os cinco critérios."""
    criteria = [
        "company_verified",
        "original_page_verified",
        "application_link_verified",
        "job_content_verified",
        "date_verified",
    ]

    return CandidateEvidence(
        company_verified=True,
        original_page_verified=True,
        application_link_verified=True,
        job_content_verified=True,
        date_verified=True,
        evidence_records=[
            make_record(criterion)
            for criterion in criteria
        ],
    )


def save_test_candidate(candidate, evidence):
    """Salva e registra somente IDs realmente criados pelo teste."""
    result = save_candidate(candidate, evidence)

    if result["status"] == "duplicate":
        raise RuntimeError(
            "Uma URL de teste já existia. "
            "O registro não será tratado como pertencente a este teste."
        )

    CREATED_IDS.append(result["id"])
    return result


def verify_persistence(expected):
    """Reconecta ao Neon e confere os registros recuperados."""
    recovered = list_candidates(limit=500)
    recovered_by_id = {
        item["id"]: item
        for item in recovered
    }

    for candidate_id, expected_status, expected_score, expected_flags in expected:
        item = recovered_by_id.get(candidate_id)

        if item is None:
            raise AssertionError(
                f"Oportunidade {candidate_id} não foi recuperada."
            )

        assert item["status"] == expected_status
        assert item["score"] == expected_score

        for field, expected_value in expected_flags.items():
            assert item["evidence"][field] is expected_value, (
                f"Evidência incorreta: {field}, "
                f"oportunidade {candidate_id}."
            )

        print(
            f"Persistência confirmada: ID={candidate_id}, "
            f"status={item['status']}, pontos={item['score']}."
        )


def cleanup():
    """Remove exclusivamente os registros criados nesta execução."""
    if not CREATED_IDS:
        print("Nenhum registro de teste precisou ser removido.")
        return

    conn = connect()

    try:
        with conn:
            for candidate_id in CREATED_IDS:
                execute(
                    conn,
                    "DELETE FROM candidates WHERE id = ?",
                    (candidate_id,),
                )

        print(
            f"Limpeza concluída: {len(CREATED_IDS)} "
            "registro(s) de teste removido(s)."
        )
    finally:
        conn.close()


def main():
    if not DATABASE_URL:
        raise RuntimeError(
            "Teste interrompido: configure DATABASE_URL "
            "com a conexão do Neon."
        )

    print("Inicializando estrutura do Neon...")
    init_db()

    expected = []

    try:
        print("Teste 1: oportunidade sem evidências.")

        empty = save_test_candidate(
            make_candidate("sem-evidencias"),
            CandidateEvidence(),
        )

        assert empty["score"] == 0
        assert empty["status"] == "review"

        expected.append((
            empty["id"],
            "review",
            0,
            {
                "company_verified": False,
                "original_page_verified": False,
                "application_link_verified": False,
                "job_content_verified": False,
                "date_verified": False,
            },
        ))

        print("Aprovado: sem evidências, permanece em revisão.")

        print("Teste 2: quatro critérios verificados.")

        partial = save_test_candidate(
            make_candidate("evidencia-parcial"),
            make_partial_evidence(),
        )

        assert partial["score"] == 85
        assert partial["status"] == "review"

        expected.append((
            partial["id"],
            "review",
            85,
            {
                "company_verified": True,
                "original_page_verified": True,
                "application_link_verified": True,
                "job_content_verified": True,
                "date_verified": False,
            },
        ))

        print(
            "Aprovado: 85 pontos não são suficientes "
            "sem os cinco critérios."
        )

        print("Teste 3: cinco critérios verificados.")

        complete = save_test_candidate(
            make_candidate("evidencia-completa"),
            make_complete_evidence(),
        )

        assert complete["score"] == 100
        assert complete["status"] == "approved"

        expected.append((
            complete["id"],
            "approved",
            100,
            {
                "company_verified": True,
                "original_page_verified": True,
                "application_link_verified": True,
                "job_content_verified": True,
                "date_verified": True,
            },
        ))

        print("Aprovado: cinco critérios, 100 pontos.")

        print("Verificando persistência em nova conexão...")
        verify_persistence(expected)

        print("\nTODOS OS TESTES DE TRIAGEM PASSARAM.")

    finally:
        cleanup()


if __name__ == "__main__":
    main()
