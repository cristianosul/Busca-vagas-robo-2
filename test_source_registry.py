
import pytest

from app.repository import update_source_status
from app import repository
from app.source_registry import (
    Source,
    get_source,
    get_sources,
    register_source,
    validate_source,
)


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(
        repository,
        "DB_PATH",
        tmp_path / "test_sources.db",
    )
    repository.init_db()


def make_source(
    source_id="empresa-exemplo",
    enabled=False,
):
    return Source(
        source_id=source_id,
        name="Empresa Exemplo",
        source_type="company",
        base_url="https://example.com/jobs",
        authorization="official_public_page",
        enabled=enabled,
        notes="Fonte de teste",
    )


def test_register_and_get_source(isolated_database):
    source = make_source()

    registered = register_source(source)
    found = get_source(source.source_id)

    assert registered == source
    assert found == source


def test_list_sources_from_database(isolated_database):
    register_source(make_source("empresa-ativa", enabled=True))
    register_source(make_source("empresa-inativa", enabled=False))

    all_sources = get_sources()
    active_sources = get_sources(enabled_only=True)

    assert len(all_sources) == 2
    assert len(active_sources) == 1
    assert active_sources[0].source_id == "empresa-ativa"


def test_duplicate_source_is_rejected(isolated_database):
    source = make_source()

    register_source(source)

    with pytest.raises(ValueError):
        register_source(source)


def test_get_missing_source_returns_none(isolated_database):
    assert get_source("nao-existe") is None


def test_validate_source_rejects_http_url():
    source = Source(
        source_id="empresa-insegura",
        name="Empresa Insegura",
        source_type="company",
        base_url="http://example.com",
        authorization="official_public_page",
    )

    with pytest.raises(ValueError):
        validate_source(source)


def test_source_status_persists_after_new_database_connection(
    isolated_database,
):
    source = make_source("empresa-persistente", enabled=True)
    register_source(source)

    updated = update_source_status(
        "empresa-persistente",
        False,
    )

    assert updated is not None
    assert not updated["enabled"]

    found = get_source("empresa-persistente")

    assert found is not None
    assert found.enabled is False

    update_source_status("empresa-persistente", True)

    found_again = get_source("empresa-persistente")

    assert found_again is not None
    assert found_again.enabled is True
