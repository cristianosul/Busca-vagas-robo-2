
import os
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
        raise RuntimeError("DATABASE_URL não configurado.")

    source_id = f"neon-test-fontes-{uuid.uuid4().hex[:12]}"

    source = Source(
        source_id=source_id,
        name="Fonte de teste Neon",
        source_type="company",
        base_url="https://example.com/neon-test",
        authorization="official_public_page",
        enabled=True,
        notes="Registro temporário para teste de persistência no Neon.",
    )

    try:
        repository.init_db()
        print("TESTE OK: Neon inicializado")

        register_source(source)
        print("TESTE OK: fonte cadastrada")

        found = get_source(source_id)
        assert found is not None
        assert found.name == source.name
        print("TESTE OK: fonte consultada")

        repository.update_source_status(source_id, False)
        found = get_source(source_id)
        assert found is not None and found.enabled is False
        print("TESTE OK: desativação persistida")

        found_enabled = get_sources(enabled_only=True)
        assert all(item.source_id != source_id for item in found_enabled)
        print("TESTE OK: fonte desativada não aparece na lista ativa")

        repository.update_source_status(source_id, True)
        found = get_source(source_id)
        assert found is not None and found.enabled is True
        print("TESTE OK: reativação persistida")

    finally:
        with repository.db_session() as conn:
            repository.execute(
                conn,
                "DELETE FROM sources WHERE source_id = ?",
                (source_id,),
            )
        print("LIMPEZA OK: fonte de teste removida")


if __name__ == "__main__":
    main()
