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
        "enabled": True,
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


def test_valid_token_can_register_and_list_source(client):
    created = client.post(
        "/api/sources",
        json=source_payload(),
        headers=admin_headers(),
    )

    assert created.status_code == 201
    assert created.json()["source_id"] == "empresa-seguranca"

    listed = client.get(
        "/api/sources",
        headers=admin_headers(),
    )

    assert listed.status_code == 200
    assert any(
        item["source_id"] == "empresa-seguranca"
        for item in listed.json()
    )


def test_can_get_source_by_id(client):
    client.post(
        "/api/sources",
        json=source_payload(),
        headers=admin_headers(),
    )

    response = client.get(
        "/api/sources/empresa-seguranca",
        headers=admin_headers(),
    )

    assert response.status_code == 200
    assert response.json()["source_id"] == "empresa-seguranca"


def test_can_activate_and_deactivate_source(client):
    client.post(
        "/api/sources",
        json=source_payload(),
        headers=admin_headers(),
    )

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

    assert activated.status_code == 200
    assert activated.json()["enabled"] in (True, 1)


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
