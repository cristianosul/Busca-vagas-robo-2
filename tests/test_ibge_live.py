
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


RUN_LIVE_IBGE_TESTS = os.getenv("RUN_LIVE_IBGE_TESTS") == "1"


def _municipality_payload():
    return [
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
    ]


def test_ibge_retries_temporary_failure_and_succeeds(monkeypatch):
    attempts = []
    sleep_calls = []

    successful_response = httpx.Response(
        200,
        json=_municipality_payload(),
        request=httpx.Request(
            "GET",
            national_discovery.IBGE_MUNICIPALITIES_URL,
        ),
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

    monkeypatch.setattr(
        national_discovery.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    response = national_discovery._request_ibge_catalog()

    assert response.status_code == 200
    assert len(attempts) == 3
    assert sleep_calls == [
        national_discovery.IBGE_RETRY_DELAY,
        national_discovery.IBGE_RETRY_DELAY * 2,
    ]


def test_ibge_raises_error_after_three_timeouts(monkeypatch):
    attempts = []
    sleep_calls = []

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

    monkeypatch.setattr(
        national_discovery.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    with pytest.raises(
        MunicipalityCatalogError,
        match="3 tentativas",
    ):
        national_discovery._request_ibge_catalog()

    assert len(attempts) == 3
    assert sleep_calls == [
        national_discovery.IBGE_RETRY_DELAY,
        national_discovery.IBGE_RETRY_DELAY * 2,
    ]


@pytest.mark.skipif(
    not RUN_LIVE_IBGE_TESTS,
    reason="Consulta real ao IBGE desativada.",
)
def test_live_ibge_catalog_has_national_scale_and_all_ufs():
    load_brazilian_municipalities.cache_clear()

    municipalities = load_brazilian_municipalities()

    assert len(municipalities) >= 5500
    assert len({item.code for item in municipalities}) == len(municipalities)
    assert {item.state for item in municipalities} == BRAZILIAN_STATE_CODES


@pytest.mark.skipif(
    not RUN_LIVE_IBGE_TESTS,
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
