
import json
from types import SimpleNamespace

import pytest

import app.national_discovery as national_discovery
from app.search_engine import SearchProviderError


# ============================================================
# Mecanismo de pesquisa simulado
# ============================================================


class FakeSearchEngine:
    """Simula o mecanismo de pesquisa sem acessar a internet."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def search(self, query, limit=10):
        self.calls.append((query, limit))

        response = self.responses.get(query, [])

        if isinstance(response, Exception):
            raise response

        return response


@pytest.fixture
def fake_engine_class(monkeypatch):
    """Permite testar o coordenador com um mecanismo simulado."""

    monkeypatch.setattr(
        national_discovery,
        "SearchEngine",
        FakeSearchEngine,
    )

    return FakeSearchEngine


@pytest.fixture(autouse=True)
def clear_municipality_cache():
    """Evita que o cache de um teste interfira em outro."""

    national_discovery.load_brazilian_municipalities.cache_clear()

    yield

    national_discovery.load_brazilian_municipalities.cache_clear()


def make_result(url, title="Vagas", snippet="Oportunidades"):
    return SimpleNamespace(
        url=url,
        title=title,
        snippet=snippet,
    )


# ============================================================
# Catálogo municipal local
# ============================================================


UF_CODES = {
    "RO": 11,
    "AC": 12,
    "AM": 13,
    "RR": 14,
    "PA": 15,
    "AP": 16,
    "TO": 17,
    "MA": 21,
    "PI": 22,
    "CE": 23,
    "RN": 24,
    "PB": 25,
    "PE": 26,
    "AL": 27,
    "SE": 28,
    "BA": 29,
    "MG": 31,
    "ES": 32,
    "RJ": 33,
    "SP": 35,
    "PR": 41,
    "SC": 42,
    "RS": 43,
    "MS": 50,
    "MT": 51,
    "GO": 52,
    "DF": 53,
}


def make_catalog_municipality(
    code,
    name,
    state,
    *,
    uf_code=None,
):
    """Cria um registro no formato do catálogo oficial local."""

    code = str(code)
    uf_code = UF_CODES.get(state) if uf_code is None else uf_code

    return {
        "codigo_ibge": code,
        "municipio": name,
        "codigo_uf": uf_code,
        "uf": state,
        "codigo_regiao_intermediaria": "4301",
        "regiao_intermediaria": "Região Intermediária de Teste",
        "codigo_regiao_imediata": "430001",
        "regiao_imediata": "Região Imediata de Teste",
        "codigo_municipio": code[-5:],
    }


def make_catalog_payload(municipalities):
    return {
        "fonte": "IBGE",
        "ano": 2025,
        "total_municipios": len(municipalities),
        "municipios": municipalities,
    }


def write_catalog(tmp_path, monkeypatch, municipalities):
    """Grava um catálogo sintético e aponta o loader para ele."""

    catalog_path = tmp_path / "municipios.json"
    payload = make_catalog_payload(municipalities)

    catalog_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    # O arquivo de produção deverá definir esta constante.
    # raising=False permite preparar o teste antes da implementação.
    monkeypatch.setattr(
        national_discovery,
        "MUNICIPALITIES_CATALOG_PATH",
        catalog_path,
        raising=False,
    )

    return catalog_path


def make_rs_catalog(count):
    """Gera registros sintéticos com códigos municipais de sete dígitos."""

    return [
        make_catalog_municipality(
            f"43{index:05d}",
            f"Municipio {index:03d}",
            "RS",
        )
        for index in range(count)
    ]


# ============================================================
# Consultas nacionais
# ============================================================


def test_build_national_queries_includes_all_categories():
    queries = (
        national_discovery.NationalDiscoveryCoordinator
        .build_national_queries()
    )

    assert len(queries) == 12
    assert len(queries) == len(set(queries))

    for category_queries in national_discovery.NATIONAL_SEARCHES.values():
        for query in category_queries:
            assert query in queries


@pytest.mark.parametrize(
    "category",
    [
        "companies",
        "agencies",
        "temporary_work",
        "headhunters",
        "ats",
        "regional_public_sources",
    ],
)
def test_build_national_queries_accepts_each_category(category):
    queries = (
        national_discovery.NationalDiscoveryCoordinator
        .build_national_queries([category])
    )

    assert queries
    assert queries == list(
        national_discovery.NATIONAL_SEARCHES[category]
    )


def test_build_national_queries_removes_repeated_categories():
    queries = (
        national_discovery.NationalDiscoveryCoordinator
        .build_national_queries(["companies", "companies"])
    )

    assert queries == list(
        national_discovery.NATIONAL_SEARCHES["companies"]
    )


@pytest.mark.parametrize(
    "categories",
    [
        [],
        "",
        ["unknown"],
        ["companies", ""],
        ["companies", 123],
    ],
)
def test_build_national_queries_rejects_invalid_categories(categories):
    with pytest.raises(ValueError):
        national_discovery.NationalDiscoveryCoordinator.build_national_queries(
            categories
        )


def test_coordinator_rejects_string_instead_of_category_list():
    with pytest.raises(ValueError):
        national_discovery.NationalDiscoveryCoordinator.build_national_queries(
            "companies"
        )


def test_coordinator_requires_search_engine(fake_engine_class):
    with pytest.raises(ValueError):
        national_discovery.NationalDiscoveryCoordinator(object())


def test_search_national_returns_results(fake_engine_class):
    query = national_discovery.NATIONAL_SEARCHES["companies"][0]

    expected_result = make_result(
        "https://empresa.example/carreiras"
    )

    engine = fake_engine_class({
        query: [expected_result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_national(
        categories=["companies"],
        limit_per_query=5,
    )

    assert report.queries == tuple(
        national_discovery.NATIONAL_SEARCHES["companies"]
    )
    assert report.results == (expected_result,)
    assert report.errors == ()
    assert engine.calls


def test_search_national_removes_duplicate_urls(fake_engine_class):
    queries = national_discovery.NATIONAL_SEARCHES["companies"]

    first_result = make_result(
        "https://empresa.example/vagas?utm_source=site"
    )
    second_result = make_result(
        "https://empresa.example/vagas"
    )

    engine = fake_engine_class({
        queries[0]: [first_result],
        queries[1]: [second_result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_national(
        categories=["companies"]
    )

    assert len(report.results) == 1
    assert report.results[0] == first_result


def test_search_national_continues_after_provider_failure(fake_engine_class):
    queries = national_discovery.NATIONAL_SEARCHES["companies"]

    expected_result = make_result(
        "https://empresa.example/carreiras"
    )

    engine = fake_engine_class({
        queries[0]: SearchProviderError("Falha simulada"),
        queries[1]: [expected_result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_national(
        categories=["companies"]
    )

    assert len(report.errors) == 1
    assert report.errors[0].query == queries[0]
    assert "Falha simulada" in report.errors[0].message
    assert report.results == (expected_result,)


def test_search_national_validates_limit(fake_engine_class):
    engine = fake_engine_class()
    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    with pytest.raises(ValueError):
        coordinator.search_national(
            categories=["companies"],
            limit_per_query=0,
        )

    with pytest.raises(ValueError):
        coordinator.search_national(
            categories=["companies"],
            limit_per_query=51,
        )

    assert engine.calls == []


# ============================================================
# Pesquisa por empresa e inspeção de página oficial
# ============================================================


def test_search_company_uses_company_queries(
    fake_engine_class,
    monkeypatch,
):
    company_queries = [
        "Empresa Exemplo carreiras",
        "Empresa Exemplo vagas",
    ]

    expected_result = make_result(
        "https://empresa.example/trabalhe-conosco"
    )

    monkeypatch.setattr(
        national_discovery,
        "build_company_queries",
        lambda company_name: company_queries,
    )

    engine = fake_engine_class({
        company_queries[0]: [expected_result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_company(
        "Empresa Exemplo",
        limit_per_query=3,
    )

    assert report.queries == tuple(company_queries)
    assert report.results == (expected_result,)
    assert report.errors == ()
    assert engine.calls == [
        (company_queries[0], 3),
        (company_queries[1], 3),
    ]


def test_inspect_official_site_delegates_to_company_discovery(monkeypatch):
    expected_pages = [
        national_discovery.CareerPageCandidate(
            company_name="Empresa Exemplo",
            page_url="https://empresa.example/carreiras",
            link_text="Trabalhe conosco",
        )
    ]

    captured = {}

    def fake_discover(company_name, official_site_url):
        captured["company_name"] = company_name
        captured["official_site_url"] = official_site_url
        return expected_pages

    monkeypatch.setattr(
        national_discovery,
        "discover_career_pages",
        fake_discover,
    )

    result = (
        national_discovery.NationalDiscoveryCoordinator
        .inspect_official_site(
            "Empresa Exemplo",
            "https://empresa.example",
        )
    )

    assert result == expected_pages
    assert captured == {
        "company_name": "Empresa Exemplo",
        "official_site_url": "https://empresa.example",
    }


def test_search_national_records_category_and_query(fake_engine_class):
    query = national_discovery.NATIONAL_SEARCHES["companies"][0]

    result = make_result(
        "https://empresa.example/carreiras?utm_source=google"
    )

    engine = fake_engine_class({
        query: [result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_national(
        categories=["companies"],
    )

    assert len(report.matches) == 1

    match = report.matches[0]

    assert match.category == "companies"
    assert match.query == query
    assert match.normalized_url == "https://empresa.example/carreiras"
    assert match.result == result


def test_search_national_preserves_duplicate_origins(fake_engine_class):
    queries = national_discovery.NATIONAL_SEARCHES["companies"]

    first_result = make_result(
        "https://empresa.example/vagas?utm_source=google"
    )

    second_result = make_result(
        "https://empresa.example/vagas"
    )

    engine = fake_engine_class({
        queries[0]: [first_result],
        queries[1]: [second_result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_national(
        categories=["companies"],
    )

    assert len(report.results) == 1
    assert len(report.matches) == 2

    assert report.matches[0].category == "companies"
    assert report.matches[0].query == queries[0]

    assert report.matches[1].category == "companies"
    assert report.matches[1].query == queries[1]

    assert (
        report.matches[0].normalized_url
        == report.matches[1].normalized_url
    )


def test_search_company_records_company_category(
    fake_engine_class,
    monkeypatch,
):
    company_queries = [
        "Empresa Exemplo carreiras",
        "Empresa Exemplo vagas",
    ]

    result = make_result(
        "https://empresa.example/trabalhe-conosco"
    )

    monkeypatch.setattr(
        national_discovery,
        "build_company_queries",
        lambda company_name: company_queries,
    )

    engine = fake_engine_class({
        company_queries[0]: [result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_company("Empresa Exemplo")

    assert len(report.matches) == 1

    match = report.matches[0]

    assert match.category == "company"
    assert match.query == company_queries[0]
    assert match.normalized_url == (
        "https://empresa.example/trabalhe-conosco"
    )
    assert match.result == result


def test_national_report_preserves_results_compatibility(fake_engine_class):
    query = national_discovery.NATIONAL_SEARCHES["companies"][0]

    result = make_result(
        "https://empresa.example/vagas"
    )

    engine = fake_engine_class({
        query: [result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_national(
        categories=["companies"],
    )

    assert report.results == (result,)
    assert isinstance(report.results, tuple)
    assert isinstance(report.matches, tuple)
    assert report.matches[0].result is result


# ============================================================
# Carregamento do catálogo municipal JSON local
# ============================================================


def test_load_brazilian_municipalities(tmp_path, monkeypatch):
    payload = [
        make_catalog_municipality("4300034", "Aceguá", "RS"),
        make_catalog_municipality("4300059", "Água Santa", "RS"),
        make_catalog_municipality("3550308", "São Paulo", "SP"),
    ]

    write_catalog(tmp_path, monkeypatch, payload)

    municipalities = (
        national_discovery.load_brazilian_municipalities()
    )

    assert len(municipalities) == 3

    by_code = {
        municipality.code: municipality
        for municipality in municipalities
    }

    assert by_code["4300034"].name == "Aceguá"
    assert by_code["4300034"].state == "RS"

    assert by_code["4300059"].name == "Água Santa"
    assert by_code["4300059"].state == "RS"

    assert by_code["3550308"].name == "São Paulo"
    assert by_code["3550308"].state == "SP"


def test_load_municipalities_preserves_small_interior_cities(
    tmp_path,
    monkeypatch,
):
    payload = [
        make_catalog_municipality("4300034", "Aceguá", "RS"),
        make_catalog_municipality("4300059", "Água Santa", "RS"),
        make_catalog_municipality("4300109", "Agudo", "RS"),
    ]

    write_catalog(tmp_path, monkeypatch, payload)

    queries = national_discovery.build_municipal_queries(0)

    municipality_names = [
        item[2]
        for item in queries
    ]

    assert "Aceguá" in municipality_names
    assert "Água Santa" in municipality_names
    assert "Agudo" in municipality_names


def test_municipal_queries_are_divided_into_batches(
    tmp_path,
    monkeypatch,
):
    payload = make_rs_catalog(121)
    write_catalog(tmp_path, monkeypatch, payload)

    first_batch = national_discovery.build_municipal_queries(0)
    second_batch = national_discovery.build_municipal_queries(1)
    third_batch = national_discovery.build_municipal_queries(2)

    assert len(first_batch) == 60
    assert len(second_batch) == 60
    assert len(third_batch) == 1

    first_codes = {
        item[2]
        for item in first_batch
    }

    second_codes = {
        item[2]
        for item in second_batch
    }

    assert first_codes.isdisjoint(second_codes)


def test_municipality_batch_count(tmp_path, monkeypatch):
    write_catalog(
        tmp_path,
        monkeypatch,
        make_rs_catalog(121),
    )

    assert national_discovery.municipality_batch_count() == 3


def test_municipal_queries_reject_invalid_batch(
    tmp_path,
    monkeypatch,
):
    payload = [
        make_catalog_municipality("4300034", "Aceguá", "RS"),
    ]

    write_catalog(tmp_path, monkeypatch, payload)

    with pytest.raises(ValueError):
        national_discovery.build_municipal_queries(-1)

    with pytest.raises(ValueError):
        national_discovery.build_municipal_queries(True)

    with pytest.raises(ValueError):
        national_discovery.build_municipal_queries(1)


def test_search_municipalities_records_city_and_state(
    fake_engine_class,
    tmp_path,
    monkeypatch,
):
    payload = [
        make_catalog_municipality("4300034", "Aceguá", "RS"),
    ]

    write_catalog(tmp_path, monkeypatch, payload)

    result = make_result(
        "https://empresa.example/vagas"
    )

    query = (
        "vagas de emprego empresas contratando agências de emprego "
        "SINE Aceguá RS"
    )

    engine = fake_engine_class({
        query: [result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(
        engine
    )

    report = coordinator.search_municipalities(batch_index=0)

    assert len(report.matches) == 1

    match = report.matches[0]

    assert match.category == "municipality:RS"
    assert match.municipality == "Aceguá"
    assert match.state == "RS"
    assert match.result == result


def test_catalog_cache_avoids_repeated_file_reads(
    tmp_path,
    monkeypatch,
):
    payload = [
        make_catalog_municipality("4300034", "Aceguá", "RS"),
    ]

    write_catalog(tmp_path, monkeypatch, payload)

    first = national_discovery.load_brazilian_municipalities()
    cache_after_first = (
        national_discovery.load_brazilian_municipalities.cache_info()
    )

    second = national_discovery.load_brazilian_municipalities()
    cache_after_second = (
        national_discovery.load_brazilian_municipalities.cache_info()
    )

    assert first == second
    assert cache_after_first.misses == 1
    assert cache_after_second.misses == 1
    assert cache_after_second.hits == cache_after_first.hits + 1


def test_missing_catalog_raises_catalog_error(tmp_path, monkeypatch):
    missing_path = tmp_path / "municipios.json"

    monkeypatch.setattr(
        national_discovery,
        "MUNICIPALITIES_CATALOG_PATH",
        missing_path,
        raising=False,
    )

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()


def test_corrupted_catalog_json_raises_catalog_error(
    tmp_path,
    monkeypatch,
):
    catalog_path = tmp_path / "municipios.json"

    catalog_path.write_text(
        '{"municipios": [',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        national_discovery,
        "MUNICIPALITIES_CATALOG_PATH",
        catalog_path,
        raising=False,
    )

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()


def test_empty_catalog_raises_catalog_error(tmp_path, monkeypatch):
    write_catalog(
        tmp_path,
        monkeypatch,
        [],
    )

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()


@pytest.mark.parametrize(
    "invalid_payload",
    [
        [],
        {"erro": "resposta inválida"},
        {"municipios": None},
        {"municipios": "não é uma lista"},
        {"municipios": []},
    ],
)
def test_catalog_rejects_invalid_root_structure(
    tmp_path,
    monkeypatch,
    invalid_payload,
):
    catalog_path = tmp_path / "municipios.json"

    catalog_path.write_text(
        json.dumps(invalid_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        national_discovery,
        "MUNICIPALITIES_CATALOG_PATH",
        catalog_path,
        raising=False,
    )

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()


@pytest.mark.parametrize(
    "invalid_municipality",
    [
        None,
        "registro inválido",
        {},
        {
            "codigo_ibge": "4300034",
            "municipio": "Cidade sem UF",
        },
        make_catalog_municipality(
            "codigo-invalido",
            "Cidade com código inválido",
            "RS",
        ),
        make_catalog_municipality(
            "4300034",
            "",
            "RS",
        ),
        make_catalog_municipality(
            "4300034",
            "Cidade sem código de UF",
            "RS",
        ) | {"codigo_uf": None},
    ],
)
def test_catalog_rejects_incomplete_or_invalid_municipalities(
    tmp_path,
    monkeypatch,
    invalid_municipality,
):
    write_catalog(
        tmp_path,
        monkeypatch,
        [invalid_municipality],
    )

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()


def test_catalog_rejects_unknown_state_code(
    tmp_path,
    monkeypatch,
):
    municipality = make_catalog_municipality(
        "9900001",
        "Cidade Teste",
        "XX",
        uf_code=99,
    )

    write_catalog(
        tmp_path,
        monkeypatch,
        [municipality],
    )

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()


def test_catalog_rejects_state_code_mismatch(
    tmp_path,
    monkeypatch,
):
    municipality = make_catalog_municipality(
        "4300034",
        "Cidade Teste",
        "RS",
        uf_code=35,
    )

    write_catalog(
        tmp_path,
        monkeypatch,
        [municipality],
    )

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()


def test_catalog_rejects_non_seven_digit_code(
    tmp_path,
    monkeypatch,
):
    municipality = make_catalog_municipality(
        "123",
        "Cidade Teste",
        "RS",
    )

    write_catalog(
        tmp_path,
        monkeypatch,
        [municipality],
    )

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()


def test_catalog_rejects_duplicate_municipality_codes(
    tmp_path,
    monkeypatch,
):
    payload = [
        make_catalog_municipality("4300034", "Aceguá", "RS"),
        make_catalog_municipality(
            "4300034",
            "Aceguá Duplicado",
            "RS",
        ),
    ]

    write_catalog(tmp_path, monkeypatch, payload)

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()


def test_all_municipal_batches_cover_catalog_without_duplicates(
    tmp_path,
    monkeypatch,
):
    payload = make_rs_catalog(121)
    write_catalog(tmp_path, monkeypatch, payload)

    batch_count = national_discovery.municipality_batch_count()

    all_batches = [
        national_discovery.build_municipal_queries(batch_index)
        for batch_index in range(batch_count)
    ]

    all_names = [
        item[2]
        for batch in all_batches
        for item in batch
    ]

    expected_names = {
        f"Municipio {index:03d}"
        for index in range(121)
    }

    assert len(all_names) == 121
    assert len(set(all_names)) == 121
    assert set(all_names) == expected_names


def test_last_municipality_batch_contains_remaining_city(
    tmp_path,
    monkeypatch,
):
    payload = make_rs_catalog(61)
    write_catalog(tmp_path, monkeypatch, payload)

    first_batch = national_discovery.build_municipal_queries(0)
    last_batch = national_discovery.build_municipal_queries(1)

    assert len(first_batch) == 60
    assert len(last_batch) == 1
    assert last_batch[0][2] == "Municipio 060"


# ============================================================
# Pesquisa individual por município
# ============================================================


def test_search_municipality_accepts_municipality_object(
    fake_engine_class,
):
    municipality = national_discovery.Municipality(
        code="4300034",
        name="Aceguá",
        state="RS",
    )

    result = make_result("https://empresa.example/vagas")

    query = (
        "vagas de emprego empresas contratando agências de emprego "
        "SINE Aceguá RS"
    )

    engine = fake_engine_class({
        query: [result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_municipality(municipality)

    assert report.results == (result,)
    assert len(engine.calls) == 1


def test_search_municipality_accepts_dictionary(fake_engine_class):
    municipality = {
        "code": "4300034",
        "name": "Aceguá",
        "state": "RS",
    }

    result = make_result("https://empresa.example/vagas")

    query = (
        "vagas de emprego empresas contratando agências de emprego "
        "SINE Aceguá RS"
    )

    engine = fake_engine_class({
        query: [result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_municipality(municipality)

    assert report.results == (result,)


def test_search_municipality_executes_only_one_query(fake_engine_class):
    municipality = {
        "code": "4300034",
        "name": "Aceguá",
        "state": "RS",
    }

    engine = fake_engine_class()
    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_municipality(municipality)

    assert len(engine.calls) == 1
    assert len(report.queries) == 1
    assert engine.calls[0][1] == 10


def test_search_municipality_records_city_and_state(fake_engine_class):
    municipality = {
        "code": "4300034",
        "name": "Aceguá",
        "state": "rs",
    }

    result = make_result("https://empresa.example/vagas")

    query = (
        "vagas de emprego empresas contratando agências de emprego "
        "SINE Aceguá RS"
    )

    engine = fake_engine_class({
        query: [result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_municipality(municipality)

    assert len(report.matches) == 1

    match = report.matches[0]

    assert match.category == "municipality:RS"
    assert match.municipality == "Aceguá"
    assert match.state == "RS"
    assert match.result == result


@pytest.mark.parametrize(
    "code",
    [
        "123",
        "abcdefg",
        4300034,
        "",
    ],
)
def test_search_municipality_rejects_invalid_code(
    fake_engine_class,
    code,
):
    municipality = {
        "code": code,
        "name": "Aceguá",
        "state": "RS",
    }

    engine = fake_engine_class()
    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    with pytest.raises(ValueError):
        coordinator.search_municipality(municipality)

    assert engine.calls == []


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        None,
        123,
    ],
)
def test_search_municipality_rejects_invalid_name(
    fake_engine_class,
    name,
):
    municipality = {
        "code": "4300034",
        "name": name,
        "state": "RS",
    }

    engine = fake_engine_class()
    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    with pytest.raises(ValueError):
        coordinator.search_municipality(municipality)

    assert engine.calls == []


@pytest.mark.parametrize(
    "state",
    [
        "",
        "R",
        "RSR",
        "12",
        None,
        123,
    ],
)
def test_search_municipality_rejects_invalid_state(
    fake_engine_class,
    state,
):
    municipality = {
        "code": "4300034",
        "name": "Aceguá",
        "state": state,
    }

    engine = fake_engine_class()
    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    with pytest.raises(ValueError):
        coordinator.search_municipality(municipality)

    assert engine.calls == []


def test_search_municipality_validates_limit(fake_engine_class):
    municipality = {
        "code": "4300034",
        "name": "Aceguá",
        "state": "RS",
    }

    engine = fake_engine_class()
    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    with pytest.raises(ValueError):
        coordinator.search_municipality(
            municipality,
            limit_per_query=0,
        )

    with pytest.raises(ValueError):
        coordinator.search_municipality(
            municipality,
            limit_per_query=51,
        )

    assert engine.calls == []


def test_search_municipality_preserves_origins_and_deduplicates(
    fake_engine_class,
):
    municipality = {
        "code": "4300034",
        "name": "Aceguá",
        "state": "RS",
    }

    query = (
        "vagas de emprego empresas contratando agências de emprego "
        "SINE Aceguá RS"
    )

    first_result = make_result(
        "https://empresa.example/vagas?utm_source=google"
    )
    second_result = make_result(
        "https://empresa.example/vagas"
    )

    engine = fake_engine_class({
        query: [first_result, second_result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_municipality(municipality)

    assert len(report.results) == 1
    assert report.results[0] == first_result
    assert len(report.matches) == 2

    assert (
        report.matches[0].normalized_url
        == report.matches[1].normalized_url
    )

    assert all(
        match.municipality == "Aceguá"
        and match.state == "RS"
        for match in report.matches
    )


# ============================================================
# Cobertura nacional explícita: regiões e unidades federativas
# ============================================================


def test_geographic_catalog_covers_all_27_federative_units():
    expected = {
        "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO",
        "MA", "MT", "MS", "MG", "PA", "PB", "PR", "PE", "PI",
        "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO",
    }

    actual = {
        category.removeprefix("state:")
        for category in national_discovery.STATE_SEARCHES
    }

    assert actual == expected
    assert national_discovery.BRAZILIAN_STATE_CODES == expected


def test_geographic_catalog_covers_all_five_regions_without_gaps_or_overlap():
    expected_regions = {
        "region:norte",
        "region:nordeste",
        "region:centro-oeste",
        "region:sudeste",
        "region:sul",
    }

    assert set(national_discovery.REGIONAL_SEARCHES) == expected_regions
    assert set(national_discovery.REGION_STATES) == expected_regions

    all_states = []

    for states in national_discovery.REGION_STATES.values():
        all_states.extend(states)

    assert len(all_states) == 27
    assert len(set(all_states)) == 27
    assert set(all_states) == national_discovery.BRAZILIAN_STATE_CODES


def test_geographic_queries_stay_within_national_query_limit():
    queries = (
        national_discovery.NationalDiscoveryCoordinator
        .build_national_queries(include_geography=True)
    )

    assert len(queries) <= national_discovery.MAX_NATIONAL_QUERIES
    assert len(queries) == len(set(queries))


def test_national_geographic_queries_include_every_state_name():
    query_items = (
        national_discovery.NationalDiscoveryCoordinator
        ._build_query_items(include_geography=True)
    )

    state_items = {
        category: query
        for category, query in query_items
        if category.startswith("state:")
    }

    assert set(state_items) == set(national_discovery.STATE_SEARCHES)

    for category, state_name in national_discovery.STATE_SEARCHES.items():
        assert state_name in state_items[category]


def test_national_geographic_queries_include_each_region():
    query_items = (
        national_discovery.NationalDiscoveryCoordinator
        ._build_query_items(include_geography=True)
    )

    region_categories = {
        category
        for category, _ in query_items
        if category.startswith("region:")
    }

    assert region_categories == set(national_discovery.REGIONAL_SEARCHES)
    
