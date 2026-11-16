
from types import SimpleNamespace

import pytest

import app.national_discovery as national_discovery
from app.search_engine import SearchProviderError


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


def make_result(url, title="Vagas", snippet="Oportunidades"):
    return SimpleNamespace(
        url=url,
        title=title,
        snippet=snippet,
    )


def test_build_national_queries_includes_all_categories():
    queries = national_discovery.NationalDiscoveryCoordinator.build_national_queries()

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
    queries = national_discovery.NationalDiscoveryCoordinator.build_national_queries(
        [category]
    )

    assert queries
    assert queries == list(
        national_discovery.NATIONAL_SEARCHES[category]
    )


def test_build_national_queries_removes_repeated_categories():
    queries = national_discovery.NationalDiscoveryCoordinator.build_national_queries(
        ["companies", "companies"]
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

    engine = fake_engine_class(
        {query: [expected_result]}
    )

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

    engine = fake_engine_class(
        {
            queries[0]: [first_result],
            queries[1]: [second_result],
        }
    )

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_national(
        categories=["companies"]
    )

    assert len(report.results) == 1
    assert report.results[0] == first_result


def test_search_national_continues_after_provider_failure(
    fake_engine_class,
):
    queries = national_discovery.NATIONAL_SEARCHES["companies"]

    expected_result = make_result(
        "https://empresa.example/carreiras"
    )

    engine = fake_engine_class(
        {
            queries[0]: SearchProviderError("Falha simulada"),
            queries[1]: [expected_result],
        }
    )

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

    engine = fake_engine_class(
        {
            company_queries[0]: [expected_result],
        }
    )

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


def test_inspect_official_site_delegates_to_company_discovery(
    monkeypatch,
):
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

    result = national_discovery.NationalDiscoveryCoordinator.inspect_official_site(
        "Empresa Exemplo",
        "https://empresa.example",
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

    # A lista principal continua eliminando duplicidades.
    assert len(report.results) == 1

    # O histórico preserva as duas descobertas.
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


def test_national_report_preserves_results_compatibility(
    fake_engine_class,
):
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


def make_ibge_municipality(code, name, state):
    return {
        "id": code,
        "nome": name,
        "regiao-imediata": {
            "regiao-intermediaria": {
                "UF": {
                    "sigla": state,
                }
            }
        },
    }


def mock_ibge_response(monkeypatch, municipalities):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return municipalities

    monkeypatch.setattr(
        national_discovery.httpx,
        "get",
        lambda *args, **kwargs: FakeResponse(),
    )


def test_load_brazilian_municipalities(monkeypatch):
    national_discovery.load_brazilian_municipalities.cache_clear()

    payload = [
        make_ibge_municipality(4300034, "Aceguá", "RS"),
        make_ibge_municipality(4300059, "Água Santa", "RS"),
        make_ibge_municipality(3550308, "São Paulo", "SP"),
    ]

    mock_ibge_response(monkeypatch, payload)

    municipalities = (
        national_discovery.load_brazilian_municipalities()
    )

    assert len(municipalities) == 3
    assert municipalities[0].code == "4300034"
    assert municipalities[0].name == "Aceguá"
    assert municipalities[0].state == "RS"

    national_discovery.load_brazilian_municipalities.cache_clear()


def test_load_municipalities_preserves_small_interior_cities(
    monkeypatch,
):
    national_discovery.load_brazilian_municipalities.cache_clear()

    payload = [
        make_ibge_municipality(4300034, "Aceguá", "RS"),
        make_ibge_municipality(4300059, "Água Santa", "RS"),
        make_ibge_municipality(4300109, "Agudo", "RS"),
    ]

    mock_ibge_response(monkeypatch, payload)

    queries = national_discovery.build_municipal_queries(0)

    municipality_names = [
        item[2]
        for item in queries
    ]

    assert "Aceguá" in municipality_names
    assert "Água Santa" in municipality_names
    assert "Agudo" in municipality_names

    national_discovery.load_brazilian_municipalities.cache_clear()
    
    
def test_municipal_queries_are_divided_into_batches(monkeypatch):
    national_discovery.load_brazilian_municipalities.cache_clear()

    payload = [
        make_ibge_municipality(
            4300000 + index,
            f"Municipio {index:03d}",
            "RS",
        )
        for index in range(121)
    ]

    mock_ibge_response(monkeypatch, payload)

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

    national_discovery.load_brazilian_municipalities.cache_clear()


def test_municipality_batch_count(monkeypatch):
    national_discovery.load_brazilian_municipalities.cache_clear()

    payload = [
        make_ibge_municipality(
            4300000 + index,
            f"Municipio {index:03d}",
            "RS",
        )
        for index in range(121)
    ]

    mock_ibge_response(monkeypatch, payload)

    assert national_discovery.municipality_batch_count() == 3

    national_discovery.load_brazilian_municipalities.cache_clear()


def test_municipal_queries_reject_invalid_batch(monkeypatch):
    national_discovery.load_brazilian_municipalities.cache_clear()

    payload = [
        make_ibge_municipality(4300034, "Aceguá", "RS"),
    ]

    mock_ibge_response(monkeypatch, payload)

    with pytest.raises(ValueError):
        national_discovery.build_municipal_queries(-1)

    with pytest.raises(ValueError):
        national_discovery.build_municipal_queries(True)

    with pytest.raises(ValueError):
        national_discovery.build_municipal_queries(1)

    national_discovery.load_brazilian_municipalities.cache_clear()


def test_search_municipalities_records_city_and_state(
    fake_engine_class,
    monkeypatch,
):
    national_discovery.load_brazilian_municipalities.cache_clear()

    payload = [
        make_ibge_municipality(4300034, "Aceguá", "RS"),
    ]

    mock_ibge_response(monkeypatch, payload)

    result = make_result(
        "https://empresa.example/vagas"
    )

    engine = fake_engine_class({
        "vagas de emprego empresas contratando agências de emprego "
        "SINE Aceguá RS": [result],
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

    national_discovery.load_brazilian_municipalities.cache_clear()


def test_ibge_invalid_response_raises_catalog_error(monkeypatch):
    national_discovery.load_brazilian_municipalities.cache_clear()

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("JSON inválido")

    monkeypatch.setattr(
        national_discovery.httpx,
        "get",
        lambda *args, **kwargs: FakeResponse(),
    )

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()

    national_discovery.load_brazilian_municipalities.cache_clear()


def test_ibge_empty_catalog_raises_error(monkeypatch):
    national_discovery.load_brazilian_municipalities.cache_clear()

    mock_ibge_response(monkeypatch, [])

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()

    national_discovery.load_brazilian_municipalities.cache_clear()


def test_ibge_cache_avoids_repeated_requests(monkeypatch):
    national_discovery.load_brazilian_municipalities.cache_clear()

    payload = [
        make_ibge_municipality(4300034, "Aceguá", "RS"),
    ]

    calls = {"count": 0}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    def fake_get(*args, **kwargs):
        calls["count"] += 1
        return FakeResponse()

    monkeypatch.setattr(
        national_discovery.httpx,
        "get",
        fake_get,
    )

    first = national_discovery.load_brazilian_municipalities()
    second = national_discovery.load_brazilian_municipalities()

    assert first == second
    assert calls["count"] == 1

    national_discovery.load_brazilian_municipalities.cache_clear()
    

def test_all_municipal_batches_cover_catalog_without_duplicates(
    monkeypatch,
):
    national_discovery.load_brazilian_municipalities.cache_clear()

    payload = [
        make_ibge_municipality(
            4300000 + index,
            f"Municipio {index:03d}",
            "RS",
        )
        for index in range(121)
    ]

    mock_ibge_response(monkeypatch, payload)

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

    national_discovery.load_brazilian_municipalities.cache_clear()


def test_last_municipality_batch_contains_remaining_city(
    monkeypatch,
):
    national_discovery.load_brazilian_municipalities.cache_clear()

    payload = [
        make_ibge_municipality(
            4300000 + index,
            f"Municipio {index:03d}",
            "RS",
        )
        for index in range(61)
    ]

    mock_ibge_response(monkeypatch, payload)

    first_batch = national_discovery.build_municipal_queries(0)
    last_batch = national_discovery.build_municipal_queries(1)

    assert len(first_batch) == 60
    assert len(last_batch) == 1
    assert last_batch[0][2] == "Municipio 060"

    national_discovery.load_brazilian_municipalities.cache_clear()


@pytest.mark.parametrize(
    "invalid_payload",
    [
        [
            {
                "id": 4300001,
                "nome": "Cidade sem UF",
            }
        ],
        [
            make_ibge_municipality(
                "codigo-invalido",
                "Cidade com código inválido",
                "RS",
            )
        ],
        [
            make_ibge_municipality(
                4300001,
                "",
                "RS",
            )
        ],
        [
            "registro inválido",
        ],
    ],
)
def test_ibge_rejects_incomplete_or_invalid_municipalities(
    monkeypatch,
    invalid_payload,
):
    national_discovery.load_brazilian_municipalities.cache_clear()

    mock_ibge_response(monkeypatch, invalid_payload)

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()

    national_discovery.load_brazilian_municipalities.cache_clear()


def test_ibge_rejects_non_list_catalog(monkeypatch):
    national_discovery.load_brazilian_municipalities.cache_clear()

    mock_ibge_response(
        monkeypatch,
        {"erro": "resposta inválida"},
    )

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()

    national_discovery.load_brazilian_municipalities.cache_clear()


def test_ibge_deduplicates_repeated_municipality_codes(monkeypatch):
    national_discovery.load_brazilian_municipalities.cache_clear()

    payload = [
        make_ibge_municipality(4300034, "Aceguá", "RS"),
        make_ibge_municipality(4300034, "Aceguá Duplicado", "RS"),
        make_ibge_municipality(4300059, "Água Santa", "RS"),
    ]

    mock_ibge_response(monkeypatch, payload)

    municipalities = (
        national_discovery.load_brazilian_municipalities()
    )

    codes = [municipality.code for municipality in municipalities]

    assert len(codes) == len(set(codes))
    assert len(municipalities) == 2
    assert "Aceguá Duplicado" not in {
        municipality.name for municipality in municipalities
    }

    national_discovery.load_brazilian_municipalities.cache_clear()


def test_load_municipalities_supports_legacy_ibge_structure(
    monkeypatch,
):
    national_discovery.load_brazilian_municipalities.cache_clear()

    payload = [
        {
            "id": 4300034,
            "nome": "Aceguá",
            "microrregiao": {
                "mesorregiao": {
                    "UF": {
                        "sigla": "RS",
                    }
                }
            },
        },
    ]

    mock_ibge_response(monkeypatch, payload)

    municipalities = (
        national_discovery.load_brazilian_municipalities()
    )

    assert len(municipalities) == 1
    assert municipalities[0].code == "4300034"
    assert municipalities[0].name == "Aceguá"
    assert municipalities[0].state == "RS"

    national_discovery.load_brazilian_municipalities.cache_clear()


def test_ibge_connection_failure_raises_catalog_error(monkeypatch):
    import httpx

    national_discovery.load_brazilian_municipalities.cache_clear()

    def fake_get(*args, **kwargs):
        raise httpx.ConnectError("Falha de conexão simulada")

    monkeypatch.setattr(
        national_discovery.httpx,
        "get",
        fake_get,
    )

    with pytest.raises(
        national_discovery.MunicipalityCatalogError
    ):
        national_discovery.load_brazilian_municipalities()

    national_discovery.load_brazilian_municipalities.cache_clear()
    
    
def test_search_municipality_accepts_municipality_object(
    fake_engine_class,
):
    municipality = national_discovery.Municipality(
        code="4300034",
        name="Aceguá",
        state="RS",
    )

    result = make_result("https://empresa.example/vagas")
    engine = fake_engine_class({
        "vagas de emprego empresas contratando agências de emprego "
        "SINE Aceguá RS": [result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_municipality(municipality)

    assert report.results == (result,)
    assert len(engine.calls) == 1


def test_search_municipality_accepts_dictionary(
    fake_engine_class,
):
    municipality = {
        "code": "4300034",
        "name": "Aceguá",
        "state": "RS",
    }

    result = make_result("https://empresa.example/vagas")
    engine = fake_engine_class({
        "vagas de emprego empresas contratando agências de emprego "
        "SINE Aceguá RS": [result],
    })

    coordinator = national_discovery.NationalDiscoveryCoordinator(engine)

    report = coordinator.search_municipality(municipality)

    assert report.results == (result,)


def test_search_municipality_executes_only_one_query(
    fake_engine_class,
):
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


def test_search_municipality_records_city_and_state(
    fake_engine_class,
):
    municipality = {
        "code": "4300034",
        "name": "Aceguá",
        "state": "rs",
    }

    result = make_result("https://empresa.example/vagas")
    engine = fake_engine_class({
        "vagas de emprego empresas contratando agências de emprego "
        "SINE Aceguá RS": [result],
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


def test_search_municipality_validates_limit(
    fake_engine_class,
):
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
    
