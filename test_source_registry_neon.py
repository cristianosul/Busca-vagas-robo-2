
import uuid

from app import repository
from app.source_registry import (
    Source,
    get_source,
    get_sources,
    register_source,
)


def main():
    if not repository.DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL não configurado. "
            "Teste interrompido para evitar uso acidental do SQLite."
        )

    source_id = f"neon-test-fontes-{uuid.uuid4().hex[:12]}"

    source = Source(
        source_id=source_id,
        name="Fonte fictícia de teste Neon",
        source_type="company",
        base_url="https://example.com/neon-test",
        authorization="official_public_page",
        enabled=True,
        notes=(
            "REGISTRO FICTÍCIO E TEMPORÁRIO. "
            "Criado exclusivamente para testar a persistência no Neon. "
            "Não representa uma fonte real autorizada."
        ),
    )

    registered = False

    try:
        # 1. Inicializa o banco e executa as migrações.
        repository.init_db()
        print("TESTE OK: Neon inicializado")

        # 2. Cadastra a fonte fictícia.
        register_source(source)
        registered = True
        print("TESTE OK: fonte fictícia cadastrada")

        # 3. Consulta a fonte cadastrada.
        found = get_source(source_id)

        assert found is not None
        assert found.name == source.name
        assert found.enabled is True

        print("TESTE OK: fonte consultada")

        # 4. Desativa a fonte e verifica a persistência.
        repository.update_source_status(source_id, False)

        found = get_source(source_id)

        assert found is not None
        assert found.enabled is False

        print("TESTE OK: desativação persistida")

        # 5. Confirma que a fonte desativada não aparece
        # na listagem de fontes ativas.
        found_enabled = get_sources(enabled_only=True)

        assert all(
            item.source_id != source_id
            for item in found_enabled
        )

        print(
            "TESTE OK: fonte desativada não aparece "
            "na lista ativa"
        )

        # 6. Reativa a fonte fictícia para testar a persistência.
        repository.update_source_status(source_id, True)

        found = get_source(source_id)

        assert found is not None
        assert found.enabled is True

        print("TESTE OK: reativação persistida")

    finally:
        # 7. Só remove a fonte se o cadastro foi confirmado.
        if registered:
            with repository.db_session() as conn:
                repository.execute(
                    conn,
                    """
                    DELETE FROM sources
                    WHERE source_id = ?
                    """,
                    (source_id,),
                )

            print("LIMPEZA OK: fonte de teste removida")

        else:
            print(
                "LIMPEZA: nenhum cadastro confirmado; "
                "nenhum registro foi removido."
            )


if __name__ == "__main__":
    main()
