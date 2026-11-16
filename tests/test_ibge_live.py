
"""Testes do catálogo público do IBGE.

Os testes de repetição usam simulações e não precisam de internet.

Execute com RUN_LIVE_IBGE_TESTS=1 para validar conectividade
e cobertura do catálogo público real do IBGE.
"""

import os

import httpx
import pytest

import app.national_discovery as national_discovery
from app.national_discovery import (
    BRAZILIAN_STATE_CODES,
    REGION_STATES,
    MunicipalityCatalogError,
    load_brazilian_municipalities,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_IBGE_TESTS") != "1",
    reason="Teste externo do IBGE desativado na suíte local.",
)


@pytest.fixture
def enable_local_retry_tests(monkeypatch):
    """Permite executar testes locais sem depender da internet."""

    monkeypatch.setattr(
        national_discovery,
        "IBGE_MAX_ATTEMPTS",
        3,
    )
    monkeypatch.setattr(
        national_discovery,
        "IBGE_RETRY_DELAY",
        0,
    )


@pytest.mark.skipif(
    False,
    reason="Os testes de repetição não dependem de acesso externo.",
)
def test_placeholder():
    """Mantido apenas para compatibilidade com a estrutura do módulo."""
    pass


def test_ibge_retries_temporary_failure_and_succeeds(
    monkeypatch,
    enable_local_retry_tests,
):
    attempts = []

    successful_response = httpx.Response(
        200,
        json=[
            {
                "id": 4305108,
                "nome": "Caxias do Sul",
                "regiao-imediata": {
                    "regiao-intermediaria": {
                        "UF": {
                            "sigla": "RS",
                        }
                    }
                },
            }
        ],
    )

    def fake_get(*args, **kwargs):
        attempts.append(1)

        if len(attempts) < 3:
            raise httpx.ConnectTimeout(
                "Falha temporária simulada."
            )

        return successful_response

    monkeypatch.setattr(
        national_discovery.httpx,
        "get",
        fake_get,
    )

    response = national_discovery._request_ibge_catalog()

    assert response.status_code == 200
    assert len(attempts) == 3


def test_ibge_raises_error_after_three_timeouts(
    monkeypatch,
    enable_local_retry_tests,
):
    attempts = []

    def fake_get(*args, **kwargs):
        attempts.append(1)
        raise httpx.ConnectTimeout(
            "Timeout simulado."
        )

    monkeypatch.setattr(
        national_discovery.httpx,
        "get",
        fake_get,
    )

    with pytest.raises(
        MunicipalityCatalogError,
        match="3 tentativas",
    ):
        national_discovery._request_ibge_catalog()

    assert len(attempts) == 3


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_IBGE_TESTS") != "1",
    reason="Consulta real ao IBGE desativada.",
)
def test_live_ibge_catalog_has_national_scale_and_all_ufs():
    load_brazilian_municipalities.cache_clear()

    municipalities = load_brazilian_municipalities()

    assert len(municipalities) >= 5500
    assert len({item.code for item in municipalities}) == len(municipalities)
    assert {item.state for item in municipalities} == BRAZILIAN_STATE_CODES


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_IBGE_TESTS") != "1",
    reason="Consulta real ao IBGE desativada.",
)
def test_live_ibge_catalog_has_municipalities_in_all_five_regions():
    load_brazilian_municipalities.cache_clear()

    municipalities = load_brazilian_municipalities()

    states_with_municipalities = {
        item.state for item in municipalities
    }

    assert set(REGION_STATES) == {
        "region:norte",
        "region:nordeste",
        "region:centro-oeste",
        "region:sudeste",
        "region:sul",
    }

    for region, states in REGION_STATES.items():
        assert states
        assert states.issubset(states_with_municipalities), region
