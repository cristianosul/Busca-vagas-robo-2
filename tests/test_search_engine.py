from types import SimpleNamespace

import httpx
import pytest

from app import search_engine as search


class FakeProvider:
    def __init__(self, name, results=None, error=None):
        self.name = name
        self.results = results or []
        self.error = error
        self.calls = []

    def search(self, query, *, limit):
        self.calls.append((query, limit))
        if self.error:
            raise self.error
        return self.results[:limit]


def make_result(url="https://empresa.example/vagas", provider=""):
    return search.SearchResult(
        title="Vagas abertas",
        url=url,
        snippet="Oportunidades de emprego",
        provider=provider,
    )


def test_normalize_search_url_removes_tracking_and_fragment():
    normalized = search.normalize_search_url(
        "HTTPS://EXAMPLE.COM/jobs?utm_source=google&fbclid=x&area=finance#apply"
    )
    assert normalized == "https://example.com/jobs?area=finance"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/jobs",
        "https://localhost/jobs",
        "https://service.localhost/jobs",
        "https://127.0.0.1/jobs",
        "https://10.0.0.1/jobs",
        "https://example.com:8443/jobs",
        "https://user:pass@example.com/jobs",
        "https:///jobs",
        "https://example.com:invalid/jobs",
    ],
)
def test_normalize_search_url_rejects_unsafe_or_invalid_urls(url):
    with pytest.raises(ValueError):
        search.normalize_search_url(url)


def test_search_result_normalizes_fields_and_url():
    result = search.SearchResult(
        title="  Vagas  ",
        url="https://EXAMPLE.com/jobs#top",
        snippet="  Descrição  ",
        provider="  busca  ",
    )
    assert result.title == "Vagas"
    assert result.snippet == "Descrição"
    assert result.provider == "busca"
    assert result.url == "https://example.com/jobs"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": ""},
        {"title": 123},
        {"title": "Vaga", "snippet": None},
        {"title": "Vaga", "provider": None},
        {"title": "x" * (search.MAX_TITLE_LENGTH + 1)},
        {"title": "Vaga", "snippet": "x" * (search.MAX_SNIPPET_LENGTH + 1)},
    ],
)
def test_search_result_rejects_invalid_fields(kwargs):
    values = {"title": "Vaga", "url": "https://example.com"}
    values.update(kwargs)
    with pytest.raises(ValueError):
        search.SearchResult(**values)


def test_tavily_provider_uses_expected_request_and_parses_results(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "results": [
                        {
                            "title": " Vagas ",
                            "url": "https://EMPRESA.example/jobs?utm_source=x",
                            "content": " descrição ",
                        },
                        {"title": None, "url": "https://empresa.example/invalid"},
                        {"title": "Outro", "url": "http://inseguro.example"},
                        {
                            "title": "Segunda vaga",
                            "url": "https://empresa.example/jobs/2",
                            "content": 42,
                        },
                    ]
                },
            )

    monkeypatch.setattr(search.httpx, "Client", FakeClient)
    provider = search.TavilySearchProvider(api_key=" chave ")
    results = provider.search("  vagas   remotas  ", limit=2)

    assert captured["url"] == search.TAVILY_ENDPOINT
    assert captured["client"] == {
        "timeout": search.TAVILY_TIMEOUT,
        "follow_redirects": False,
        "trust_env": False,
    }
    assert captured["headers"]["Authorization"] == "Bearer chave"
    assert captured["payload"]["query"] == "vagas remotas"
    assert captured["payload"]["max_results"] == 2
    assert [item.title for item in results] == ["Vagas", "Segunda vaga"]
    assert results[0].url == "https://empresa.example/jobs"
    assert results[1].snippet == ""
    assert all(item.provider == "tavily" for item in results)


def test_tavily_provider_reads_api_key_from_environment(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", " chave-env ")
    assert search.TavilySearchProvider().api_key == "chave-env"


@pytest.mark.parametrize("query,limit", [(None, 1), ("ab", 1), ("x" * 301, 1), ("vagas", 0), ("vagas", 21), ("vagas", True)])
def test_tavily_provider_validates_query_and_limit_before_network(query, limit):
    provider = search.TavilySearchProvider(api_key="teste")
    with pytest.raises(ValueError):
        provider.search(query, limit=limit)


def test_tavily_provider_requires_api_key():
    with pytest.raises(search.SearchProviderError, match="TAVILY_API_KEY"):
        search.TavilySearchProvider(api_key="").search("vagas", limit=1)


@pytest.mark.parametrize(
    "status,message",
    [
        (302, "redirecionamento"),
        (401, "autenticação"),
        (403, "autenticação"),
        (429, "Limite"),
        (500, "indisponível"),
        (400, "HTTP 400"),
    ],
)
def test_tavily_provider_maps_http_errors(monkeypatch, status, message):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=status)

    monkeypatch.setattr(search.httpx, "Client", FakeClient)
    with pytest.raises(search.SearchProviderError, match=message):
        search.TavilySearchProvider(api_key="teste").search("vagas", limit=1)


@pytest.mark.parametrize(
    "exception,expected",
    [
        (httpx.TimeoutException("timeout"), "demorou demais"),
        (httpx.ConnectError("offline"), "conectar"),
    ],
)
def test_tavily_provider_maps_network_errors(monkeypatch, exception, expected):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            raise exception

    monkeypatch.setattr(search.httpx, "Client", FakeClient)
    with pytest.raises(search.SearchProviderError, match=expected):
        search.TavilySearchProvider(api_key="teste").search("vagas", limit=1)


@pytest.mark.parametrize(
    "payload",
    [
        ValueError("json inválido"),
        [],
        {},
        {"results": {}},
    ],
)
def test_tavily_provider_rejects_invalid_response_payload(monkeypatch, payload):
    def json_response():
        if isinstance(payload, Exception):
            raise payload
        return payload

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=200, json=json_response)

    monkeypatch.setattr(search.httpx, "Client", FakeClient)
    with pytest.raises(search.SearchProviderError):
        search.TavilySearchProvider(api_key="teste").search("vagas", limit=1)


def test_search_engine_normalizes_query_deduplicates_and_limits_results():
    first = FakeProvider("primeiro", [make_result("https://empresa.example/jobs")])
    second = FakeProvider(
        "segundo",
        [
            make_result("https://EMPRESA.example/jobs#top", "segundo"),
            make_result("https://outra.example/jobs", "segundo"),
        ],
    )

    results = search.SearchEngine([first, second]).search("  vagas   no Brasil ", limit=2)

    # O segundo provedor recebe apenas a quantidade ainda necessária;
    # como seu primeiro resultado é duplicado, a lista final fica com um item.
    assert [item.url for item in results] == [
        "https://empresa.example/jobs",
    ]
    assert first.calls == [("vagas no Brasil", 2)]
    assert second.calls == [("vagas no Brasil", 1)]


def test_search_engine_falls_back_after_provider_error():
    failing = FakeProvider("falha", error=search.SearchProviderError("indisponível"))
    succeeding = FakeProvider("alternativo", [make_result(provider="alternativo")])

    results = search.SearchEngine([failing, succeeding]).search("vagas", limit=3)

    assert len(results) == 1
    assert succeeding.calls == [("vagas", 3)]


def test_search_engine_raises_when_all_providers_fail():
    providers = [
        FakeProvider("um", error=search.SearchProviderError("falha 1")),
        FakeProvider("dois", error=search.SearchProviderError("falha 2")),
    ]
    with pytest.raises(search.SearchProviderError, match="Todos os provedores"):
        search.SearchEngine(providers).search("vagas")


def test_search_engine_returns_empty_when_provider_succeeds_without_results():
    provider = FakeProvider("vazio")
    assert search.SearchEngine([provider]).search("vagas") == []


def test_search_engine_rejects_invalid_provider_result():
    provider = FakeProvider("quebrado", [object()])
    with pytest.raises(TypeError, match="resultado inválido"):
        search.SearchEngine([provider]).search("vagas")


def test_search_engine_fills_missing_result_provider_name():
    provider = FakeProvider("fonte-teste", [make_result(provider="")])
    result = search.SearchEngine([provider]).search("vagas")[0]
    assert result.provider == "fonte-teste"


def test_search_engine_rejects_invalid_inputs_and_missing_providers():
    engine = search.SearchEngine([FakeProvider("ok")])
    for query in (None, "ab", "x" * 301):
        with pytest.raises(ValueError):
            engine.search(query)
    for limit in (0, 51, True, 1.5):
        with pytest.raises(ValueError):
            engine.search("vagas", limit=limit)
    with pytest.raises(RuntimeError, match="Nenhum provedor"):
        search.SearchEngine([]).search("vagas")


def test_build_company_queries_returns_four_normalized_queries():
    queries = search.build_company_queries("  Empresa   Exemplo ")
    assert queries == [
        '"Empresa Exemplo" trabalhe conosco',
        '"Empresa Exemplo" carreiras vagas',
        '"Empresa Exemplo" oportunidades de emprego',
        '"Empresa Exemplo" página oficial de carreiras',
    ]


@pytest.mark.parametrize("name", [None, "", "x", "a" * 151])
def test_build_company_queries_rejects_invalid_company_name(name):
    with pytest.raises(ValueError):
        search.build_company_queries(name)
