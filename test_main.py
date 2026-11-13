
import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(monkeypatch):
    # Impede conexão com o Neon durante os testes.
    monkeypatch.setattr(main, "init_db", lambda: None)

    with TestClient(main.app) as test_client:
        yield test_client


def candidate_payload():
    return {
        "title": "Analista de Sistemas",
        "company": "Empresa Teste",
        "location": "Caxias do Sul - RS",
        "mode": "Híbrido",
        "employment_type": "Efetivo (CLT)",
        "source_name": "Página oficial",
        "source_url": "https://empresa.com/carreiras",
        "original_job_url": "https://empresa.com/vagas/123",
        "application_url": "https://empresa.com/candidatar/123",
    }


def source_payload():
    return {
        "source_id": "empresa-teste",
        "name": "Empresa Teste",
        "source_type": "company",
        "base_url": "https://empresa.com",
        "authorization": "official_public_page",
        "enabled": False,
    }


# =========================
# HEALTH E ROTA PRINCIPAL
# =========================

def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["publishing_enabled"] is False


def test_root(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["publishing_enabled"] is False


# =========================
# CADASTRO DE CANDIDATOS
# =========================

def test_create_candidate(client, monkeypatch):
    monkeypatch.setattr(
        main,
        "save_candidate",
        lambda candidate: {
            "title": candidate.title,
            "company": candidate.company,
        },
    )

    response = client.post(
        "/api/candidates",
        json=candidate_payload(),
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Analista de Sistemas"


def test_create_candidate_invalid_data(client):
    response = client.post(
        "/api/candidates",
        json={"title": "AB"},
    )

    assert response.status_code == 422


def test_create_candidate_rejects_extra_fields(client):
    payload = candidate_payload()
    payload["company_verified"] = True

    response = client.post(
        "/api/candidates",
        json=payload,
    )

    assert response.status_code == 422


def test_create_candidate_value_error(client, monkeypatch):
    def fail(candidate):
        raise ValueError("Oportunidade inválida")

    monkeypatch.setattr(main, "save_candidate", fail)

    response = client.post(
        "/api/candidates",
        json=candidate_payload(),
    )

    assert response.status_code == 422


def test_create_candidate_internal_error(client, monkeypatch):
    def fail(candidate):
        raise RuntimeError("Erro interno")

    monkeypatch.setattr(main, "save_candidate", fail)

    response = client.post(
        "/api/candidates",
        json=candidate_payload(),
    )

    assert response.status_code == 500


# =========================
# CONSULTA DE CANDIDATOS
# =========================

def test_get_candidates(client, monkeypatch):
    monkeypatch.setattr(
        main,
        "list_candidates",
        lambda status, limit: [],
    )

    response = client.get("/api/candidates")

    assert response.status_code == 200
    assert response.json() == []


def test_get_candidates_with_filters(client, monkeypatch):
    def fake_list(status, limit):
        return [{"status": status, "limit": limit}]

    monkeypatch.setattr(
        main,
        "list_candidates",
        fake_list,
    )

    response = client.get(
        "/api/candidates?status=approved&limit=20"
    )

    assert response.status_code == 200
    assert response.json()[0]["status"] == "approved"
    assert response.json()[0]["limit"] == 20


def test_get_candidates_invalid_status(client):
    response = client.get(
        "/api/candidates?status=invalid"
    )

    assert response.status_code == 422


def test_get_candidates_invalid_limit(client):
    response = client.get(
        "/api/candidates?limit=0"
    )

    assert response.status_code == 422


def test_get_candidates_internal_error(client, monkeypatch):
    def fail(status, limit):
        raise RuntimeError("Erro no banco")

    monkeypatch.setattr(main, "list_candidates", fail)

    response = client.get("/api/candidates")

    assert response.status_code == 500


# =========================
# AUTENTICAÇÃO DE FONTES
# =========================

def test_sources_without_configuration(client, monkeypatch):
    monkeypatch.delenv("SOURCE_ADMIN_TOKEN", raising=False)

    response = client.get("/api/sources")

    assert response.status_code == 503


def test_sources_without_token(client, monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    response = client.get("/api/sources")

    assert response.status_code == 401


def test_sources_invalid_token(client, monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    response = client.get(
        "/api/sources",
        headers={"X-Admin-Token": "token-errado"},
    )

    assert response.status_code == 401


# =========================
# CADASTRO DE FONTES
# =========================

def test_create_source(client, monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    monkeypatch.setattr(
        main,
        "save_source",
        lambda source: {"source_id": source.source_id},
    )

    response = client.post(
        "/api/sources",
        json=source_payload(),
        headers={"X-Admin-Token": "token-secreto"},
    )

    assert response.status_code == 201
    assert response.json()["source_id"] == "empresa-teste"


def test_create_source_rejects_http(client, monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    payload = source_payload()
    payload["base_url"] = "http://empresa.com"

    response = client.post(
        "/api/sources",
        json=payload,
        headers={"X-Admin-Token": "token-secreto"},
    )

    assert response.status_code == 422


def test_create_source_duplicate(client, monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    def fail(source):
        raise ValueError("Fonte já cadastrada")

    monkeypatch.setattr(main, "save_source", fail)

    response = client.post(
        "/api/sources",
        json=source_payload(),
        headers={"X-Admin-Token": "token-secreto"},
    )

    assert response.status_code == 409


# =========================
# CONSULTA DE FONTES
# =========================

def test_get_sources(client, monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    monkeypatch.setattr(
        main,
        "list_sources",
        lambda enabled_only, limit: [],
    )

    response = client.get(
        "/api/sources",
        headers={"X-Admin-Token": "token-secreto"},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_get_source_not_found(client, monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    monkeypatch.setattr(
        main,
        "get_source_record",
        lambda source_id: None,
    )

    response = client.get(
        "/api/sources/inexistente",
        headers={"X-Admin-Token": "token-secreto"},
    )

    assert response.status_code == 404


# =========================
# ATUALIZAÇÃO DE FONTES
# =========================

def test_update_source_status(client, monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    monkeypatch.setattr(
        main,
        "update_source_status",
        lambda source_id, enabled: {
            "source_id": source_id,
            "enabled": enabled,
        },
    )

    response = client.patch(
        "/api/sources/empresa-teste",
        json={"enabled": True},
        headers={"X-Admin-Token": "token-secreto"},
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is True


def test_update_source_not_found(client, monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    monkeypatch.setattr(
        main,
        "update_source_status",
        lambda source_id, enabled: None,
    )

    response = client.patch(
        "/api/sources/inexistente",
        json={"enabled": True},
        headers={"X-Admin-Token": "token-secreto"},
    )

    assert response.status_code == 404


def test_update_source_rejects_extra_fields(client):
    response = client.patch(
        "/api/sources/empresa-teste",
        json={
            "enabled": True,
            "name": "Alteração não permitida",
        },
        headers={"X-Admin-Token": "token-secreto"},
    )

    assert response.status_code == 422


# =========================
# INICIALIZAÇÃO DO BANCO
# =========================

def test_database_initialization_failure(monkeypatch):
    def fail():
        raise RuntimeError("Banco indisponível")

    monkeypatch.setattr(main, "init_db", fail)

    with pytest.raises(RuntimeError):
        with TestClient(main.app):
            pass


# =========================
# PESQUISA WEB COM TAVILY
# =========================

def test_discovery_without_admin_configuration(client, monkeypatch):
    monkeypatch.delenv("SOURCE_ADMIN_TOKEN", raising=False)

    response = client.post(
        "/api/discovery/search?query=empresas+de+tecnologia"
    )

    assert response.status_code == 503


def test_discovery_without_token(client, monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    response = client.post(
        "/api/discovery/search?query=empresas+de+tecnologia"
    )

    assert response.status_code == 401


def test_discovery_invalid_token(client, monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    response = client.post(
        "/api/discovery/search?query=empresas+de+tecnologia",
        headers={"X-Admin-Token": "token-errado"},
    )

    assert response.status_code == 401


def test_discovery_success_without_real_api_call(
    client,
    monkeypatch,
):
    from app.search_engine import SearchResult

    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    captured = {}

    class FakeTavilyProvider:
        name = "tavily"

        def search(self, query, *, limit):
            captured["query"] = query
            captured["limit"] = limit

            return [
                SearchResult(
                    title="Trabalhe conosco - Empresa Teste",
                    url="https://empresa.com/carreiras",
                    snippet="Página oficial de oportunidades.",
                    provider="tavily",
                )
            ]

    monkeypatch.setattr(
        main,
        "TavilySearchProvider",
        FakeTavilyProvider,
    )

    response = client.post(
        "/api/discovery/search?query=Empresa+Teste+carreiras&limit=5",
        headers={"X-Admin-Token": "token-secreto"},
    )

    assert response.status_code == 200

    data = response.json()

    assert data["provider"] == "tavily"
    assert data["total"] == 1
    assert data["saved"] is False
    assert data["publishing_enabled"] is False
    assert data["results"][0]["title"] == (
        "Trabalhe conosco - Empresa Teste"
    )
    assert data["results"][0]["url"] == (
        "https://empresa.com/carreiras"
    )

    assert captured["query"] == "Empresa Teste carreiras"
    assert captured["limit"] == 5


def test_discovery_missing_api_key(client, monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    class FakeTavilyProvider:
        name = "tavily"

        def search(self, query, *, limit):
            raise RuntimeError(
                "A chave TAVILY_API_KEY não foi configurada."
            )

    monkeypatch.setattr(
        main,
        "TavilySearchProvider",
        FakeTavilyProvider,
    )

    response = client.post(
        "/api/discovery/search?query=empresas+de+tecnologia",
        headers={"X-Admin-Token": "token-secreto"},
    )

    assert response.status_code == 503
    assert "TAVILY_API_KEY" in response.json()["detail"]


def test_discovery_rejects_limit_above_maximum(
    client,
    monkeypatch,
):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-secreto")

    response = client.post(
        "/api/discovery/search?query=empresas+de+tecnologia&limit=21",
        headers={"X-Admin-Token": "token-secreto"},
    )

    assert response.status_code == 422
    
