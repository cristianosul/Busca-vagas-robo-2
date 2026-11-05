
from app.models import CandidateIn
from app.repository import (
    connect,
    execute,
    normalize_job_url,
    save_candidate,
)

TEST_URL = "https://example.com/rob2-neon-integration-test-20261105"


def main():
    candidate = CandidateIn(
        title="Vaga fictícia para teste do Robô 2",
        company="Empresa de Teste",
        location="Brasil",
        mode="Não informado",
        source_name="Teste interno",
        source_url="https://example.com/careers",
        original_job_url=TEST_URL,
        application_url=None,
    )

    normalized_url = normalize_job_url(TEST_URL)

    try:
        save_candidate(candidate)
        save_candidate(candidate)

        with connect() as conn:
            row = execute(
                conn,
                """
                SELECT COUNT(*) AS total, MIN(score) AS score,
                       MIN(status) AS status
                FROM candidates
                WHERE normalized_url = ?
                """,
                (normalized_url,),
            ).fetchone()

        assert row["total"] == 1, "Falha na deduplicação"
        assert row["score"] == 0, "Pontuação inesperada"
        assert row["status"] == "review", "Status inesperado"

        print("TESTE OK: Neon conectado")
        print("TESTE OK: vaga recebida")
        print("TESTE OK: duplicidade bloqueada")
        print("TESTE OK: vaga permaneceu em revisão")

    finally:
        with connect() as conn:
            execute(
                conn,
                "DELETE FROM candidates WHERE normalized_url = ?",
                (normalized_url,),
            )

        print("LIMPEZA OK: vaga fictícia removida")


if __name__ == "__main__":
    main()
