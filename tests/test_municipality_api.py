
import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture
def municipality_client(monkeypatch):
    """Cria cliente sem inicializar ou acessar o banco real."""

    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(
        main,
        "init_municipality_scheduler",
        lambda: None,
    )

    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def admin_headers(monkeypatch):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-teste-seguro")
    return {"X-Admin-Token": "token-teste-seguro"}


def test_municipality_endpoint_requires_admin_token(
    municipality_client,
    monkeypatch,
):
    monkeypatch.delenv("SOURCE_ADMIN_TOKEN", raising=False)

    response = municipality_client.get(
        "/admin/municipalities/batches/current"
    )

    assert response.status_code == 503


def test_municipality_endpoint_rejects_invalid_token(
    municipality_client,
    monkeypatch,
):
    monkeypatch.setenv("SOURCE_ADMIN_TOKEN", "token-correto")

    response = municipality_client.get(
        "/admin/municipalities/batches/current",
        headers={"X-Admin-Token": "token-incorreto"},
    )

    assert response.status_code == 401


def test_current_municipality_batch(
    municipality_client,
    admin_headers,
    monkeypatch,
):
    monkeypatch.setattr(
        main,
        "get_current_batch",
        lambda: 3,
    )

    response = municipality_client.get(
        "/admin/municipalities/batches/current",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json() == {"current_batch": 3}


def test_municipality_batch_progress(
    municipality_client,
    admin_headers,
    monkeypatch,
):
    expected_progress = {
        "batch_index": 2,
        "total": 60,
        "pending": 50,
        "running": 5,
        "completed": 4,
        "failed": 1,
    }

    def fake_progress(batch_index):
        assert batch_index == 2
        return expected_progress

    monkeypatch.setattr(
        main,
        "get_batch_progress",
        fake_progress,
    )

    response = municipality_client.get(
        "/admin/municipalities/batches/2/progress",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json() == expected_progress


def test_list_municipalities_with_status_filter(
    municipality_client,
    admin_headers,
    monkeypatch,
):
    expected_municipalities = [
        {
            "code": "4305108",
            "name": "Caxias do Sul",
            "state": "RS",
            "status": "pending",
        }
    ]

    def fake_list(batch_index, status=None):
        assert batch_index == 1
        assert status == "pending"
        return expected_municipalities

    monkeypatch.setattr(
        main,
        "list_batch_municipalities",
        fake_list,
    )

    response = municipality_client.get(
        "/admin/municipalities/batches/1?status=pending",
        headers=admin_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["batch_index"] == 1
    assert data["status_filter"] == "pending"
    assert data["total"] == 1
    assert data["municipalities"] == expected_municipalities


def test_list_municipalities_rejects_invalid_status(
    municipality_client,
    admin_headers,
):
    response = municipality_client.get(
        "/admin/municipalities/batches/1?status=invalid",
        headers=admin_headers,
    )

    assert response.status_code == 422


def test_register_municipality_batch_registers_at_most_60(
    municipality_client,
    admin_headers,
    monkeypatch,
):
    municipalities = [
        {
            "code": f"{index:07d}",
            "name": f"Município {index}",
            "state": "RS",
        }
        for index in range(60)
    ]

    captured = {}

    def fake_register(batch_index, selected_municipalities):
        captured["batch_index"] = batch_index
        captured["municipalities"] = selected_municipalities

        return {
            "batch_index": batch_index,
            "total": len(selected_municipalities),
            "pending": len(selected_municipalities),
        }

    monkeypatch.setattr(
        main,
        "municipality_batch_count",
        lambda: 2,
    )
    monkeypatch.setattr(
        main,
        "load_brazilian_municipalities",
        lambda: municipalities,
    )
    monkeypatch.setattr(
        main,
        "register_municipality_batch",
        fake_register,
    )

    response = municipality_client.post(
        "/admin/municipalities/batches/0/register",
        headers=admin_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["batch_index"] == 0
    assert data["municipalities_in_catalog"] == 60
    assert data["research_started"] is False
    assert data["publishing_enabled"] is False

    assert captured["batch_index"] == 0
    assert len(captured["municipalities"]) == 60


def test_register_municipality_batch_rejects_out_of_range(
    municipality_client,
    admin_headers,
    monkeypatch,
):
    monkeypatch.setattr(
        main,
        "municipality_batch_count",
        lambda: 2,
    )

    response = municipality_client.post(
        "/admin/municipalities/batches/2/register",
        headers=admin_headers,
    )

    assert response.status_code == 404


def test_register_municipality_batch_handles_ibge_failure(
    municipality_client,
    admin_headers,
    monkeypatch,
):
    def fail_catalog():
        raise main.MunicipalityCatalogError(
            "Falha simulada no catálogo do IBGE."
        )

    monkeypatch.setattr(
        main,
        "municipality_batch_count",
        fail_catalog,
    )

    response = municipality_client.post(
        "/admin/municipalities/batches/0/register",
        headers=admin_headers,
    )

    assert response.status_code == 503
    assert "IBGE" in response.json()["detail"]
  
