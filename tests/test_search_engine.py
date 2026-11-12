import pytest

from app.search_engine import (
    SearchEngine,
    SearchResult,
    build_company_queries,
    normalize_search_url,
)


class FakeProvider:
    def __init__(self, name, results):
        self.name = name
        self.results = results
        self.queries = []

    def search(self, query, *, limit):
        self.queries.append((query, limit))
        return self.results[:limit]


def test_normalize_search_url_removes_tracking_parameters():
    result = normalize_search_url(
        "https://EXAMPLE.COM/jobs?utm_source=google&id=10&fbclid=abc#top"
    )

    assert result == "https://example.com/jobs?id=10"


def test_normalize_search_url_adds_root_path():
    assert normalize_search_url("https://example.com") == (
        "https://example.com/"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "javascript:alert(1)",
        "https://localhost",
        "https://example.com:8080",
        "https://user:password@example.com",
        "https://127.0.0.1",
        "https://192.168.1.1",
        "",
    ],
)
def test_normalize_search_url_rejects_invalid_urls(url):
    with pytest.raises(ValueError):
        normalize_search_url(url)


def test_search_result_normalizes_fields():
    result = SearchResult(
        title="  Analista de Sistemas  ",
        url="https://EXAMPLE.COM/jobs",
        snippet="  Vaga disponível  ",
        provider="  Example  ",
    )

    assert result.title == "Analista de Sistemas"
    assert result.url == "https://example.com/jobs"
    assert result.snippet == "Vaga disponível"
    assert result.provider == "Example"


def test_search_result_rejects_empty_title():
    with pytest.raises(ValueError, match="título"):
        SearchResult(
            title=" ",
            url="https://example.com",
        )


def test_search_result_rejects_long_title():
    with pytest.raises(ValueError, match="muito longo"):
        SearchResult(
            title="A" * 301,
            url="https://example.com",
        )


def test_search_result_rejects_long_snippet():
    with pytest.raises(ValueError, match="muito longa"):
        SearchResult(
            title="Vaga",
            url="https://example.com",
            snippet="A" * 2001,
        )


def test_search_engine_returns_provider_results():
    provider = FakeProvider(
        "Example",
        [
            SearchResult(
                title="Desenvolvedor Python",
                url="https://example.com/jobs/1",
            ),
        ],
    )

    engine = SearchEngine([provider])

    results = engine.search("vagas Python")

    assert len(results) == 1
    assert results[0].title == "Desenvolvedor Python"
    assert results[0].provider == "Example"


def test_search_engine_removes_duplicate_urls():
    first = FakeProvider(
        "First",
        [
            SearchResult(
                title="Vaga original",
                url="https://example.com/jobs/1",
            ),
        ],
    )

    second = FakeProvider(
        "Second",
        [
            SearchResult(
                title="Vaga duplicada",
                url="https://example.com/jobs/1",
            ),
        ],
    )

    engine = SearchEngine([first, second])

    results = engine.search("vagas")

    assert len(results) == 1
    assert results[0].title == "Vaga original"


def test_search_engine_respects_result_limit():
    provider = FakeProvider(
        "Example",
        [
            SearchResult(
                title=f"Vaga {number}",
                url=f"https://example.com/jobs/{number}",
            )
            for number in range(5)
        ],
    )

    engine = SearchEngine([provider])

    results = engine.search("vagas", limit=2)

    assert len(results) == 2
    assert provider.queries == [("vagas", 2)]


def test_search_engine_uses_multiple_providers():
    first = FakeProvider(
        "First",
        [
            SearchResult(
                title="Vaga 1",
                url="https://example.com/jobs/1",
            ),
        ],
    )

    second = FakeProvider(
        "Second",
        [
            SearchResult(
                title="Vaga 2",
                url="https://example.com/jobs/2",
            ),
        ],
    )

    engine = SearchEngine([first, second])

    results = engine.search("vagas", limit=2)

    assert len(results) == 2
    assert results[0].provider == "First"
    assert results[1].provider == "Second"


def test_search_engine_rejects_empty_query():
    engine = SearchEngine(
        [FakeProvider("Example", [])]
    )

    with pytest.raises(ValueError, match="3 caracteres"):
        engine.search("  ")


def test_search_engine_rejects_short_query():
    engine = SearchEngine(
        [FakeProvider("Example", [])]
    )

    with pytest.raises(ValueError, match="3 caracteres"):
        engine.search("ab")


def test_search_engine_rejects_long_query():
    engine = SearchEngine(
        [FakeProvider("Example", [])]
    )

    with pytest.raises(ValueError, match="ultrapassou"):
        engine.search("A" * 301)


@pytest.mark.parametrize("limit", [0, -1, 51, True, 1.5])
def test_search_engine_rejects_invalid_limit(limit):
    engine = SearchEngine(
        [FakeProvider("Example", [])]
    )

    with pytest.raises(ValueError):
        engine.search("vagas", limit=limit)


def test_search_engine_requires_provider():
    engine = SearchEngine([])

    with pytest.raises(RuntimeError, match="Nenhum provedor"):
        engine.search("vagas")


def test_search_engine_rejects_invalid_provider_result():
    provider = FakeProvider("Example", ["resultado inválido"])

    engine = SearchEngine([provider])

    with pytest.raises(TypeError, match="resultado inválido"):
        engine.search("vagas")


def test_build_company_queries_returns_four_queries():
    queries = build_company_queries("  Empresa Exemplo  ")

    assert len(queries) == 4
    assert all("Empresa Exemplo" in query for query in queries)


def test_build_company_queries_rejects_empty_name():
    with pytest.raises(ValueError, match="válido"):
        build_company_queries("  ")


def test_build_company_queries_rejects_long_name():
    with pytest.raises(ValueError, match="muito longo"):
        build_company_queries("A" * 151)


@pytest.mark.parametrize(
    "query",
    [None, 123, [], {}],
)
def test_search_engine_rejects_non_string_query(query):
    engine = SearchEngine(
        [FakeProvider("Example", [])]
    )

    with pytest.raises(ValueError, match="texto"):
        engine.search(query)


@pytest.mark.parametrize(
    "title",
    [None, 123, [], {}],
)
def test_search_result_rejects_non_string_title(title):
    with pytest.raises(ValueError, match="título"):
        SearchResult(
            title=title,
            url="https://example.com",
        )


@pytest.mark.parametrize(
    "snippet",
    [None, 123, [], {}],
)
def test_search_result_rejects_non_string_snippet(snippet):
    with pytest.raises(ValueError, match="descrição"):
        SearchResult(
            title="Vaga",
            url="https://example.com",
            snippet=snippet,
        )


@pytest.mark.parametrize(
    "provider",
    [None, 123, [], {}],
)
def test_search_result_rejects_non_string_provider(provider):
    with pytest.raises(ValueError, match="provedor"):
        SearchResult(
            title="Vaga",
            url="https://example.com",
            provider=provider,
        )


@pytest.mark.parametrize(
    "company_name",
    [None, 123, [], {}],
)
def test_build_company_queries_rejects_non_string_name(company_name):
    with pytest.raises(ValueError, match="texto"):
        build_company_queries(company_name)


def test_normalize_search_url_supports_public_ipv6():
    result = normalize_search_url(
        "https://[2606:4700:4700::1111]/jobs"
    )

    assert result == "https://[2606:4700:4700::1111]/jobs"
