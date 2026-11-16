
"""Testes de integridade do catálogo municipal local do IBGE."""

from app.national_discovery import (
    BRAZILIAN_STATE_CODES,
    REGION_STATES,
    load_brazilian_municipalities,
)


def test_local_ibge_catalog_has_national_scale_and_all_ufs():
    load_brazilian_municipalities.cache_clear()

    municipalities = load_brazilian_municipalities()

    assert len(municipalities) >= 5500
    assert len({item.code for item in municipalities}) == len(municipalities)
    assert {item.state for item in municipalities} == BRAZILIAN_STATE_CODES


def test_local_ibge_catalog_has_municipalities_in_all_five_regions():
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
