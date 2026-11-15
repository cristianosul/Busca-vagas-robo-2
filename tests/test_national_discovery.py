
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
    
