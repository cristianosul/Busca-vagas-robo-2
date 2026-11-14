
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
        # 1. Verificar conexão real com o banco.
        with connect() as conn:
            result = execute(
                conn,
                "SELECT 1 AS connection_ok",
            ).fetchone()

            assert result["connection_ok"] == 1, (
                "O PostgreSQL não confirmou a conexão"
            )

        print("TESTE OK: conexão com PostgreSQL")

        # 2. Verificar se as colunas necessárias existem.
        stage = "verificação das migrações"

        with connect() as conn:
            rows = execute(
                conn,
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = ?
                """,
                ("candidates",),
            ).fetchall()

        columns = {row["column_name"] for row in rows}

        required_columns = {
            "normalized_url",
            "evidence_json",
        }

        missing_columns = required_columns - columns

        assert not missing_columns, (
            "Colunas ausentes na tabela candidates: "
            + ", ".join(sorted(missing_columns))
        )

        print("TESTE OK: migrações e colunas necessárias")

        # 3. Inserir uma vaga fictícia.
        stage = "inserção da vaga de teste"

        first = save_candidate(candidate)

        if first["status"] == "duplicate":
            raise RuntimeError(
                "URL de teste já cadastrada. "
                "Nenhum registro existente será removido."
            )

        candidate_id = first["id"]

        assert candidate_id is not None, (
            "A inserção não retornou um ID"
        )

        print("TESTE OK: vaga fictícia inserida")

        # 4. Verificar bloqueio de duplicidade.
        stage = "verificação de duplicidade"

        second = save_candidate(candidate)

        assert second["status"] == "duplicate", (
            "A segunda inserção não foi identificada como duplicada"
        )

        assert second["id"] == candidate_id, (
            "A duplicidade retornou um ID diferente"
        )

        print("TESTE OK: duplicidade bloqueada")

        # 5. Conferir os dados efetivamente gravados.
        stage = "verificação dos dados persistidos"

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

        assert row["total"] == 1, (
            "Falha na deduplicação: quantidade de registros inesperada"
        )

        assert row["score"] == 0, (
            "Pontuação inesperada"
        )

        assert row["status"] == "review", (
            "A vaga deveria permanecer em revisão"
        )

        print("TESTE OK: vaga permaneceu em revisão")
        print("INTEGRAÇÃO NEON: TODOS OS TESTES APROVADOS")

    except Exception as exc:
        raise RuntimeError(
            f"Falha na etapa '{stage}': {exc}"
        ) from exc

    finally:
        if candidate_id is not None:
            try:
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

            except Exception as cleanup_error:
                raise RuntimeError(
                    "Falha ao remover o registro de teste. "
                    f"ID: {candidate_id}. Erro: {cleanup_error}"
                ) from cleanup_error


if __name__ == "__main__":
    main()
    
