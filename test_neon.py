
import json
import uuid

from app.models import CandidateEvidence, CandidateIn
from app.repository import (
    DATABASE_URL,
    connect,
    execute,
    init_db,
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
    stage = "conexão com PostgreSQL"

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
        # 1. Confirma a conexão real com PostgreSQL.
        with connect() as conn:
            result = execute(conn, "SELECT 1").fetchone()

        assert result is not None, (
            "O PostgreSQL não retornou resultado para SELECT 1."
        )

        print("TESTE OK: conexão com PostgreSQL")

        # 2. Executa as migrações antes de qualquer inserção.
        stage = "execução das migrações"

        init_db()

        print("TESTE OK: migrações executadas")

        # 3. Confirma a existência das colunas necessárias.
        stage = "verificação das colunas"

        required_columns = {
            "normalized_url",
            "evidence_json",
        }

        with connect() as conn:
            rows = execute(
                conn,
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'candidates'
                """,
            ).fetchall()

        existing_columns = {
            row["column_name"] for row in rows
        }

        missing_columns = required_columns - existing_columns

        if missing_columns:
            raise RuntimeError(
                "Colunas ausentes após a migração: "
                + ", ".join(sorted(missing_columns))
            )

        print("TESTE OK: colunas necessárias existem")

        # 4. Confirma o índice único de deduplicação.
        stage = "verificação do índice único"

        with connect() as conn:
            indexes = execute(
                conn,
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'candidates'
                  AND indexname = 'ux_candidates_normalized_url'
                """,
            ).fetchall()

        if not indexes:
            raise RuntimeError(
                "Índice único ux_candidates_normalized_url "
                "não encontrado."
            )

        print("TESTE OK: índice único encontrado")

        # 5. Insere uma oportunidade fictícia.
        stage = "inserção da oportunidade"

        first = save_candidate(candidate)

        if first["status"] == "duplicate":
            raise RuntimeError(
                "URL de teste já cadastrada. "
                "Nenhum registro existente será removido."
            )

        candidate_id = first["id"]

        print("TESTE OK: oportunidade fictícia inserida")

        # 6. Verifica o bloqueio de duplicidade.
        stage = "verificação de duplicidade"

        second = save_candidate(candidate)

        assert second["status"] == "duplicate", (
            "A segunda inserção não foi identificada como duplicada."
        )

        assert second["id"] == candidate_id, (
            "A duplicidade retornou um ID diferente."
        )

        print("TESTE OK: duplicidade bloqueada")

        # 7. Confirma a persistência em uma conexão independente.
        stage = "verificação da persistência"

        with connect() as conn:
            row = execute(
                conn,
                """
                SELECT
                    id,
                    normalized_url,
                    score,
                    status,
                    evidence_json
                FROM candidates
                WHERE id = ?
                  AND normalized_url = ?
                """,
                (candidate_id, normalized_url),
            ).fetchone()

        assert row is not None, (
            "O registro não foi encontrado em uma segunda conexão."
        )

        assert row["id"] == candidate_id
        assert row["normalized_url"] == normalized_url
        assert row["score"] == 0
        assert row["status"] == "review"

        evidence_data = json.loads(
            row["evidence_json"] or "{}"
        )

        CandidateEvidence.model_validate(evidence_data)

        print("TESTE OK: persistência confirmada")
        print("TESTE OK: evidências armazenadas e válidas")
        print("TESTE OK: oportunidade permaneceu em revisão")

    except Exception as exc:
        raise RuntimeError(
            f"Falha na etapa '{stage}': {exc}"
        ) from exc

    finally:
        # Remove somente o registro criado por este teste.
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
    
