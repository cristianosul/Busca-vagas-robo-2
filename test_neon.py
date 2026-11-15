
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
        # 1. Confirmar conexão real com PostgreSQL.
        with connect() as conn:
            result = execute(
                conn,
                "SELECT 1 AS connection_ok",
            ).fetchone()

            assert result["connection_ok"] == 1, (
                "O PostgreSQL não confirmou a conexão."
            )

        print("TESTE OK: conexão com PostgreSQL")

        # 2. Executar as migrações oficiais do repositório.
        stage = "execução das migrações"

        init_db()

        print("TESTE OK: migrações executadas")

        # 3. Conferir as colunas necessárias.
        stage = "verificação das colunas"

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
            "id",
            "title",
            "company",
            "location",
            "mode",
            "source_name",
            "source_url",
            "original_job_url",
            "application_url",
            "normalized_url",
            "score",
            "status",
            "reasons_json",
            "payload_json",
            "evidence_json",
            "created_at",
        }

        missing_columns = required_columns - columns

        assert not missing_columns, (
            "Colunas ausentes na tabela candidates: "
            + ", ".join(sorted(missing_columns))
        )

        print("TESTE OK: estrutura da tabela candidates")

        # 4. Confirmar a existência de índice único
        # sobre normalized_url.
        stage = "verificação do índice único"

        with connect() as conn:
            indexes = execute(
                conn,
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'candidates'
                """,
            ).fetchall()

        has_unique_normalized_index = any(
            "CREATE UNIQUE INDEX" in row["indexdef"].upper()
            and "normalized_url" in row["indexdef"].lower()
            for row in indexes
        )

        assert has_unique_normalized_index, (
            "Não foi encontrado índice único para normalized_url."
        )

        print("TESTE OK: índice único de deduplicação")

        # 5. Inserir uma vaga fictícia.
        stage = "inserção da vaga de teste"

        first = save_candidate(candidate)

        if first["status"] == "duplicate":
            raise RuntimeError(
                "A URL de teste já estava cadastrada. "
                "Nenhum registro existente será removido."
            )

        candidate_id = first["id"]

        assert candidate_id is not None, (
            "A inserção não retornou um ID."
        )

        print("TESTE OK: vaga fictícia inserida")

        # 6. Verificar duplicidade.
        stage = "verificação de duplicidade"

        second = save_candidate(candidate)

        assert second["status"] == "duplicate", (
            "A segunda inserção não foi identificada como duplicada."
        )

        assert second["id"] == candidate_id, (
            "A duplicidade retornou um ID diferente."
        )

        print("TESTE OK: duplicidade bloqueada")

        # 7. Conferir persistência em conexão independente.
        stage = "verificação dos dados persistidos"

        with connect() as conn:
            row = execute(
                conn,
                """
                SELECT id,
                       title,
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
            "A vaga não foi encontrada em uma nova conexão."
        )

        assert row["id"] == candidate_id
        assert row["title"] == candidate.title
        assert row["normalized_url"] == normalized_url
        assert row["score"] == 0
        assert row["status"] == "review"

        print("TESTE OK: persistência confirmada em nova conexão")

        # 8. Validar as evidências armazenadas.
        stage = "verificação das evidências persistidas"

        evidence_data = json.loads(row["evidence_json"] or "{}")
        evidence = CandidateEvidence.model_validate(evidence_data)

        evidence_flags = (
            "company_verified",
            "original_page_verified",
            "application_link_verified",
            "job_content_verified",
            "date_verified",
        )

        assert all(
            getattr(evidence, field) is False
            for field in evidence_flags
        ), "A vaga fictícia não deveria ter critérios verificados."

        assert not evidence.evidence_records, (
            "A vaga fictícia não deveria possuir registros de evidência."
        )

        print("TESTE OK: evidências persistidas e não verificadas")
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
                    f"ID: {candidate_id}. "
                    f"Erro: {cleanup_error}"
                ) from cleanup_error


if __name__ == "__main__":
    main()
    
