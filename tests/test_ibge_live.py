"""Testes opcionais contra o catálogo público real do IBGE.

Execute com RUN_LIVE_IBGE_TESTS=1 para validar conectividade e cobertura.
"""

import os

import pytest

from app.national_discovery import (
    BRAZILIAN_STATE_CODES,
    REGION_STATES,
    load_brazilian_municipalities,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_IBGE_TESTS") != "1",
    reason="Teste externo do IBGE desativado na suíte local.",
)


def test_live_ibge_catalog_has_national_scale_and_all_ufs():
    load_brazilian_municipalities.cache_clear()
    municipalities = load_brazilian_municipalities()

    assert len(municipalities) >= 5500
    assert len({item.code for item in municipalities}) == len(municipalities)
    assert {item.state for item in municipalities} == BRAZILIAN_STATE_CODES


def test_live_ibge_catalog_has_municipalities_in_all_five_regions():
    load_brazilian_municipalities.cache_clear()
    municipalities = load_brazilian_municipalities()
    states_with_municipalities = {item.state for item in municipalities}

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
