
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

import app.repository as repository
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Usa somente SQLite temporário.
    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(
        repository,
        "DB_PATH",
        tmp_path / "test_auth.db",
    )

    # Token fictício exclusivo para os testes.
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-teste-seguro-123")

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


def test_valid_token_can_register_and_list_source(client):
    headers = {
        "X-Admin-Token": "token-teste-seguro-123"
    }

    created = client.post(
        "/api/sources",
        json=source_payload(),
        headers=headers,
    )

    assert created.status_code == 201
    assert created.json()["source_id"] == "empresa-seguranca"

    listed = client.get(
        "/api/sources",
        headers=headers,
    )

    assert listed.status_code == 200
    assert any(
        item["source_id"] == "empresa-seguranca"
        for item in listed.json()
    )
