
import pytest

from app.models import SourceCreate
from app.repository import (
    init_db,
    save_source,
    get_source_record,
    list_sources,
    update_source_status,
)


def make_source(source_id="empresa-teste"):
    return SourceCreate(
        source_id=source_id,
        name="Empresa de Teste",
        source_type="company",
        base_url="https://example.com/careers",
        authorization="official_public_page",
        enabled=True,
    )


def test_source_persistence_and_status():
    init_db()

    source = make_source()

    try:
        saved = save_source(source)
    except ValueError:
        # Permite que o teste seja repetido sem deixar duplicatas.
        existing = get_source_record(source.source_id)
        assert existing is not None
        saved = existing

    assert saved["source_id"] == "empresa-teste"

    found = get_source_record("empresa-teste")
    assert found is not None
    assert found["name"] == "Empresa de Teste"

    sources = list_sources(enabled_only=True)
    assert any(item["source_id"] == "empresa-teste" for item in sources)

    updated = update_source_status("empresa-teste", False)
    assert updated is not None
    assert updated["enabled"] is False

    disabled_sources = list_sources(enabled_only=True)
    assert not any(
        item["source_id"] == "empresa-teste"
        for item in disabled_sources
    )


def test_duplicate_source_is_rejected():
    init_db()

    source = make_source("empresa-duplicada")
    save_source(source)

    with pytest.raises(ValueError):
        save_source(source)


def test_missing_source_returns_none():
    init_db()

    assert get_source_record("fonte-que-nao-existe") is None
    assert update_source_status("fonte-que-nao-existe", False) is None
