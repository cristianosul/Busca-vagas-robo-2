
from app.source_registry import (
    SOURCES,
    Source,
    get_source,
    get_sources,
    register_source,
)


def main():
    SOURCES.clear()

    source = Source(
        source_id="empresa-teste",
        name="Empresa de Teste",
        source_type="company",
        base_url="https://example.com/careers",
        authorization="official_public_page",
        enabled=True,
    )

    registered = register_source(source)

    assert registered == source
    assert get_source("empresa-teste") == source
    assert len(get_sources()) == 1
    assert len(get_sources(enabled_only=True)) == 1

    print("TESTE OK: cadastro de fonte")
    print("TESTE OK: consulta por identificador")
    print("TESTE OK: filtro de fontes ativadas")

    try:
        register_source(source)
        raise AssertionError("Duplicidade não foi bloqueada")
    except ValueError:
        print("TESTE OK: identificador duplicado bloqueado")

    invalid_source = Source(
        source_id="fonte-invalida",
        name="Fonte inválida",
        source_type="company",
        base_url="http://example.com",
        authorization="official_public_page",
    )

    try:
        register_source(invalid_source)
        raise AssertionError("URL HTTP deveria ser rejeitada")
    except ValueError:
        print("TESTE OK: URL sem HTTPS rejeitada")

    print("TODOS OS TESTES DO CADASTRO PASSARAM")


if __name__ == "__main__":
    main()
