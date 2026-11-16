from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.national_discovery import (
    DiscoveryMatch,
    DiscoveryQueryError,
    NationalDiscoveryReport,
)
from app.search_engine import SearchProviderError


@pytest.fixture
def discovery_client(monkeypatch):
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "init_municipality_scheduler", lambda: None)
    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def discovery_admin_headers(monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-descoberta-teste")
    return {"X-Admin-Token": "token-descoberta-teste"}


def test_national_discovery_requires_admin_token(discovery_client, monkeypatch):
    monkeypatch.delenv("SOURCE_ADMIN_TOKEN", raising=False)
    response = discovery_client.post("/api/discovery/national")
    assert response.status_code == 503


def test_national_discovery_rejects_invalid_admin_token(
    discovery_client,
    monkeypatch,
):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-correto")
    response = discovery_client.post(
        "/api/discovery/national",
        headers={"X-Admin-Token": "token-incorreto"},
    )
    assert response.status_code == 401


def test_national_discovery_returns_results_and_all_origin_matches(
    discovery_client,
    discovery_admin_headers,
    monkeypatch,
):
    result = SimpleNamespace(
        title="Vagas Empresa Exemplo",
        url="https://empresa.example/carreiras",
        snippet="Página oficial",
        provider="tavily",
    )
    report = NationalDiscoveryReport(
        queries=("consulta A", "consulta B"),
        results=(result,),
        errors=(DiscoveryQueryError("consulta C", "falha parcial"),),
        matches=(
            DiscoveryMatch("companies", "consulta A", result.url, result),
            DiscoveryMatch("ats", "consulta B", result.url, result),
        ),
    )

    class FakeCoordinator:
        def __init__(self, search_engine):
            pass

        def search_national(self, **kwargs):
            assert kwargs["categories"] == ["companies", "ats"]
            assert kwargs["include_geography"] is True
            assert kwargs["limit_per_query"] == 5
            return report

    monkeypatch.setattr(main, "NationalDiscoveryCoordinator", FakeCoordinator)
    monkeypatch.setattr(main, "SearchEngine", lambda providers: object())
    monkeypatch.setattr(main, "TavilySearchProvider", lambda: object())

    response = discovery_client.post(
        "/api/discovery/national?categories=companies&categories=ats&limit=5&include_geography=true",
        headers=discovery_admin_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total_queries"] == 2
    assert data["total_results"] == 1
    assert data["total_matches"] == 2
    assert [item["category"] for item in data["matches"]] == [
        "companies", "ats"
    ]
    assert data["errors"][0]["query"] == "consulta C"
    assert data["saved"] is False
    assert data["publishing_enabled"] is False


def test_national_discovery_maps_invalid_categories_to_422(
    discovery_client,
    discovery_admin_headers,
    monkeypatch,
):
    class FakeCoordinator:
        def __init__(self, search_engine):
            pass

        def search_national(self, **kwargs):
            raise ValueError("Categoria de pesquisa desconhecida")

    monkeypatch.setattr(main, "NationalDiscoveryCoordinator", FakeCoordinator)
    monkeypatch.setattr(main, "SearchEngine", lambda providers: object())
    monkeypatch.setattr(main, "TavilySearchProvider", lambda: object())

    response = discovery_client.post(
        "/api/discovery/national?categories=desconhecida",
        headers=discovery_admin_headers,
    )
    assert response.status_code == 422


def test_national_discovery_maps_missing_provider_key_to_503(
    discovery_client,
    discovery_admin_headers,
    monkeypatch,
):
    class FakeCoordinator:
        def __init__(self, search_engine):
            pass

        def search_national(self, **kwargs):
            raise SearchProviderError("A chave TAVILY_API_KEY não foi configurada.")

    monkeypatch.setattr(main, "NationalDiscoveryCoordinator", FakeCoordinator)
    monkeypatch.setattr(main, "SearchEngine", lambda providers: object())
    monkeypatch.setattr(main, "TavilySearchProvider", lambda: object())

    response = discovery_client.post(
        "/api/discovery/national",
        headers=discovery_admin_headers,
    )
    assert response.status_code == 503


def test_company_discovery_returns_search_provenance(
    discovery_client,
    discovery_admin_headers,
    monkeypatch,
):
    result = SimpleNamespace(
        title="Carreiras Exemplo",
        url="https://empresa.example/jobs",
        snippet="Trabalhe conosco",
        provider="tavily",
    )
    report = NationalDiscoveryReport(
        queries=('"Empresa Exemplo" trabalhe conosco',),
        results=(result,),
        errors=(),
        matches=(DiscoveryMatch("company", "consulta", result.url, result),),
    )

    class FakeCoordinator:
        def __init__(self, search_engine):
            pass

        def search_company(self, **kwargs):
            assert kwargs["company_name"] == "Empresa Exemplo"
            return report

    monkeypatch.setattr(main, "NationalDiscoveryCoordinator", FakeCoordinator)
    monkeypatch.setattr(main, "SearchEngine", lambda providers: object())
    monkeypatch.setattr(main, "TavilySearchProvider", lambda: object())

    response = discovery_client.post(
        "/api/discovery/company?company_name=Empresa%20Exemplo",
        headers=discovery_admin_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total_results"] == 1
    assert data["matches"][0]["category"] == "company"
    assert data["saved"] is False


def test_company_career_page_inspection_delegates_to_safe_discovery(
    discovery_client,
    discovery_admin_headers,
    monkeypatch,
):
    candidate = SimpleNamespace(
        company_name="Empresa Exemplo",
        page_url="https://empresa.example/carreiras",
        link_text="Trabalhe conosco",
    )
    captured = {}

    def fake_discover(company_name, official_site_url):
        captured["company_name"] = company_name
        captured["official_site_url"] = official_site_url
        return [candidate]

    monkeypatch.setattr(main, "discover_career_pages", fake_discover)

    response = discovery_client.post(
        "/api/discovery/company/career-pages"
        "?company_name=Empresa%20Exemplo"
        "&official_site_url=https%3A%2F%2Fempresa.example",
        headers=discovery_admin_headers,
    )

    assert response.status_code == 200
    assert captured == {
        "company_name": "Empresa Exemplo",
        "official_site_url": "https://empresa.example",
    }
    assert response.json()["career_pages"][0]["page_url"] == candidate.page_url
    assert response.json()["saved"] is False


def test_company_career_page_inspection_rejects_non_https_url(
    discovery_client,
    discovery_admin_headers,
    monkeypatch,
):
    def reject_url(company_name, official_site_url):
        raise ValueError("O site precisa utilizar HTTPS.")

    monkeypatch.setattr(main, "discover_career_pages", reject_url)
    response = discovery_client.post(
        "/api/discovery/company/career-pages"
        "?company_name=Empresa%20Exemplo"
        "&official_site_url=http%3A%2F%2Fempresa.example",
        headers=discovery_admin_headers,
    )
    assert response.status_code == 422


def test_discovery_endpoints_do_not_require_or_trigger_persistence(
    discovery_client,
    discovery_admin_headers,
    monkeypatch,
):
    result = SimpleNamespace(
        title="Vagas",
        url="https://empresa.example/vagas",
        snippet="",
        provider="tavily",
    )
    report = NationalDiscoveryReport(
        queries=("consulta",),
        results=(result,),
        errors=(),
        matches=(DiscoveryMatch("companies", "consulta", result.url, result),),
    )

    class FakeCoordinator:
        def __init__(self, search_engine):
            pass

        def search_national(self, **kwargs):
            return report

    def forbidden(*args, **kwargs):
        raise AssertionError("Descoberta não deve gravar dados automaticamente")

    monkeypatch.setattr(main, "NationalDiscoveryCoordinator", FakeCoordinator)
    monkeypatch.setattr(main, "SearchEngine", lambda providers: object())
    monkeypatch.setattr(main, "TavilySearchProvider", lambda: object())
    monkeypatch.setattr(main, "save_candidate", forbidden)
    monkeypatch.setattr(main, "save_source", forbidden)

    response = discovery_client.post(
        "/api/discovery/national",
        headers=discovery_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["saved"] is False
    assert response.json()["publishing_enabled"] is False
