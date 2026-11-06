
import pytest

import app.repository as repository
from app.models import SourceCreate


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    # Impede conexão com o Neon durante estes testes.
    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(
        repository,
        "DB_PATH",
        str(tmp_path / "test_sources.db"),
    )

    repository.init_db()


def make_source(source_id="empresa-teste"):
    return SourceCreate(
        source_id=source_id,
        name="Empresa de Teste",
        source_type="company",
        base_url="https://example.com/careers",
        authorization="official_public_page",
        enabled=True,
    )


def test_source_persistence_and_status(isolated_db):
    source = make_source()

    saved = repository.save_source(source)
    assert saved["source_id"] == "empresa-teste"

    found = repository.get_source_record("empresa-teste")
    assert found is not None
    assert found["name"] == "Empresa de Teste"

    sources = repository.list_sources(enabled_only=True)
    assert any(item["source_id"] == "empresa-teste" for item in sources)

    updated = repository.update_source_status("empresa-teste", False)
    assert updated is not None
    assert updated["enabled"] in (False, 0)

    enabled_sources = repository.list_sources(enabled_only=True)
    assert not any(
        item["source_id"] == "empresa-teste"
        for item in enabled_sources
    )


def test_duplicate_source_is_rejected(isolated_db):
    source = make_source("empresa-duplicada")

    repository.save_source(source)

    with pytest.raises(ValueError):
        repository.save_source(source)


def test_missing_source_returns_none(isolated_db):
    assert repository.get_source_record("fonte-inexistente") is None

    assert (
        repository.update_source_status(
            "fonte-inexistente",
            False,
        )
        is None
    )
