
import pytest
from fastapi.testclient import TestClient

import app.repository as repository
from app.main import app


ADMIN_TOKEN = "token-teste-seguro-123"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(
        repository,
        "DB_PATH",
        tmp_path / "test_auth.db",
    )

    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", ADMIN_TOKEN)

    with TestClient(app) as test_client:
        yield test_client


def source_payload():
    return {
        "source_id": "empresa-seguranca",
        "name": "Empresa de Segurança",
        "source_type": "company",
        "base_url": "https://example.com/careers",
        "authorization": "official_public_page",
        "enabled": False,
    }


def admin_headers():
    return {"X-Admin-Token": ADMIN_TOKEN}


def test_access_without_token_is_denied(client):
    response = client.post(
        "/api/sources",
        json=source_payload(),
    )
    assert response.status_code == 401


def test_access_with_wrong_token_is_denied(client):
    response = client.post(
        "/api/sources",
        json=source_payload(),
        headers={"X-Admin-Token": "token-incorreto"},
    )
    assert response.status_code == 401


def test_missing_admin_configuration_returns_503(client, monkeypatch):
    monkeypatch.delenv("SOURCE_ADMIN_TOKEN")

    response = client.get("/api/sources")
    assert response.status_code == 503


def test_register_and_list_disabled_source(client):
    created = client.post(
        "/api/sources",
        json=source_payload(),
        headers=admin_headers(),
    )

    assert created.status_code == 201
    assert created.json()["source_id"] == "empresa-seguranca"
    assert created.json()["enabled"] in (False, 0)

    listed = client.get(
        "/api/sources",
        headers=admin_headers(),
    )

    assert listed.status_code == 200
    assert any(
        item["source_id"] == "empresa-seguranca"
        for item in listed.json()
    )


def test_registration_with_enabled_true_is_blocked(client):
    payload = source_payload()
    payload["enabled"] = True

    response = client.post(
        "/api/sources",
        json=payload,
        headers=admin_headers(),
    )

    assert response.status_code == 403
    assert "desativada" in response.json()["detail"].lower()


def test_can_get_source_by_id(client):
    created = client.post(
        "/api/sources",
        json=source_payload(),
        headers=admin_headers(),
    )

    assert created.status_code == 201

    response = client.get(
        "/api/sources/empresa-seguranca",
        headers=admin_headers(),
    )

    assert response.status_code == 200
    assert response.json()["source_id"] == "empresa-seguranca"
    assert response.json()["enabled"] in (False, 0)


def test_activation_is_blocked_and_deactivation_is_allowed(client):
    created = client.post(
        "/api/sources",
        json=source_payload(),
        headers=admin_headers(),
    )

    assert created.status_code == 201

    deactivated = client.patch(
        "/api/sources/empresa-seguranca",
        json={"enabled": False},
        headers=admin_headers(),
    )

    assert deactivated.status_code == 200
    assert deactivated.json()["enabled"] in (False, 0)

    activated = client.patch(
        "/api/sources/empresa-seguranca",
        json={"enabled": True},
        headers=admin_headers(),
    )

    assert activated.status_code == 403
    assert "verificação interna" in activated.json()["detail"].lower()


def test_missing_source_returns_404(client):
    response = client.get(
        "/api/sources/fonte-inexistente",
        headers=admin_headers(),
    )

    assert response.status_code == 404


def test_updating_missing_source_returns_404(client):
    response = client.patch(
        "/api/sources/fonte-inexistente",
        json={"enabled": True},
        headers=admin_headers(),
    )

    assert response.status_code == 404
    
