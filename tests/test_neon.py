
import uuid

from app.models import CandidateIn
from app.repository import (
    DATABASE_URL,
    connect,
    execute,
    normalize_job_url,
    save_candidate,
)


TEST_URL = (
    "https://example.com/rob2-neon-integration-test-"
    + uuid.uuid4().hex
)


def main():
    if not DATABASE_URL:
        raise RuntimeError(
            "Teste interrompido: DATABASE_URL não configurada. "
            "Nenhum teste foi executado no SQLite local."
        )

    candidate_id = None
    normalized_url = normalize_job_url(TEST_URL)

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

    try:
        first = save_candidate(candidate)

        if first["status"] == "duplicate":
            raise RuntimeError(
                "URL de teste já cadastrada. "
                "Nenhum registro existente será removido."
            )

        candidate_id = first["id"]

        second = save_candidate(candidate)

        assert second["status"] == "duplicate", (
            "A segunda inserção não foi identificada como duplicada"
        )

        assert second["id"] == candidate_id, (
            "A duplicidade retornou um ID diferente"
        )

        with connect() as conn:
            row = execute(
                conn,
                """
                SELECT COUNT(*) AS total,
                       MIN(score) AS score,
                       MIN(status) AS status
                FROM candidates
                WHERE id = ?
                  AND normalized_url = ?
                """,
                (candidate_id, normalized_url),
            ).fetchone()

        assert row["total"] == 1, "Falha na deduplicação"
        assert row["score"] == 0, "Pontuação inesperada"
        assert row["status"] == "review", "Status inesperado"

        print("TESTE OK: conexão com PostgreSQL")
        print("TESTE OK: vaga fictícia inserida")
        print("TESTE OK: duplicidade bloqueada")
        print("TESTE OK: vaga permaneceu em revisão")

    finally:
        if candidate_id is not None:
            with connect() as conn:
                execute(
                    conn,
                    """
                    DELETE FROM candidates
                    WHERE id = ?
                      AND normalized_url = ?
                    """,
                    (candidate_id, normalized_url),
                )

            print("LIMPEZA OK: registro de teste removido")


if __name__ == "__main__":
    main()
