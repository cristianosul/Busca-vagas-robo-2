
import ipaddress
import os
from dataclasses import dataclass
from typing import Protocol, Sequence
from urllib.parse import (
    parse_qsl,
    urlencode,
    urlsplit,
    urlunsplit,
)

import httpx


MAX_QUERY_LENGTH = 300
MAX_RESULTS = 50
MAX_TITLE_LENGTH = 300
MAX_SNIPPET_LENGTH = 2000

TAVILY_ENDPOINT = "https://api.tavily.com/search"
TAVILY_TIMEOUT = 15.0
TAVILY_MAX_RESULTS = 20

TRACKING_PARAMETERS = {
    "gclid",
    "fbclid",
    "dclid",
    "ref",
    "source",
}


def normalize_search_url(url: str) -> str:
    """Valida e normaliza uma URL HTTPS encontrada na pesquisa."""

    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL de pesquisa inválida.")

    parsed = urlsplit(url.strip())

    if parsed.scheme.lower() != "https":
        raise ValueError("Somente URLs HTTPS são permitidas.")

    if not parsed.hostname:
        raise ValueError("A URL precisa possuir um domínio.")

    if parsed.username or parsed.password:
        raise ValueError("A URL não pode conter credenciais.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Porta inválida na URL.") from exc

    if port not in (None, 443):
        raise ValueError("Somente a porta HTTPS padrão é permitida.")

    host = parsed.hostname.lower().rstrip(".")

    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Domínio local não permitido.")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None

    if address is not None and not address.is_global:
        raise ValueError("Endereço IP não público.")

    query_parameters = []

    for key, value in parse_qsl(
        parsed.query,
        keep_blank_values=True,
    ):
        normalized_key = key.lower()

        if normalized_key.startswith("utm_"):
            continue

        if normalized_key in TRACKING_PARAMETERS:
            continue

        query_parameters.append((key, value))

    normalized_query = urlencode(query_parameters)

    netloc = f"[{host}]" if ":" in host else host

    return urlunsplit((
        "https",
        netloc,
        parsed.path or "/",
        normalized_query,
        "",
    ))


@dataclass(frozen=True)
class SearchResult:
    """Resultado encontrado por um provedor de pesquisa."""

    title: str
    url: str
    snippet: str = ""
    provider: str = ""

    def __post_init__(self):
        if not isinstance(self.title, str):
            raise ValueError("O título precisa ser um texto.")

        if not isinstance(self.snippet, str):
            raise ValueError("A descrição precisa ser um texto.")

        if not isinstance(self.provider, str):
            raise ValueError("O provedor precisa ser um texto.")

        title = self.title.strip()
        snippet = self.snippet.strip()
        provider = self.provider.strip()

        if not title:
            raise ValueError("O resultado precisa possuir um título.")

        if len(title) > MAX_TITLE_LENGTH:
            raise ValueError("Título do resultado muito longo.")

        if len(snippet) > MAX_SNIPPET_LENGTH:
            raise ValueError("Descrição do resultado muito longa.")

        object.__setattr__(self, "title", title)
        object.__setattr__(self, "snippet", snippet)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(
            self,
            "url",
            normalize_search_url(self.url),
        )


class SearchProvider(Protocol):
    """Contrato que todos os provedores de pesquisa devem implementar."""

    name: str

    def search(
        self,
        query: str,
        *,
        limit: int,
    ) -> Sequence[SearchResult]:
        ...


class TavilySearchProvider:
    """Provedor de pesquisa web usando a API oficial do Tavily."""

    name = "tavily"

    def __init__(self, api_key: str | None = None):
        self.api_key = (
            api_key
            if api_key is not None
            else os.getenv("TAVILY_API_KEY", "")
        ).strip()

    def search(
        self,
        query: str,
        *,
        limit: int,
    ) -> Sequence[SearchResult]:

        if not isinstance(query, str):
            raise ValueError("A pesquisa precisa ser um texto.")

        query = " ".join(query.split())

        if len(query) < 3 or len(query) > MAX_QUERY_LENGTH:
            raise ValueError("Texto de pesquisa fora do limite permitido.")

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= TAVILY_MAX_RESULTS
        ):
            raise ValueError(
                f"O Tavily permite entre 1 e {TAVILY_MAX_RESULTS} resultados."
            )

        if not self.api_key:
            raise RuntimeError(
                "A chave TAVILY_API_KEY não foi configurada."
            )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "query": query,
            "search_depth": "basic",
            "topic": "general",
            "max_results": limit,
            "include_answer": False,
            "include_raw_content": False,
        }

        try:
            with httpx.Client(
                timeout=TAVILY_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.post(
                    TAVILY_ENDPOINT,
                    headers=headers,
                    json=payload,
                )

        except httpx.TimeoutException as exc:
            raise RuntimeError(
                "O Tavily demorou demais para responder."
            ) from exc

        except httpx.RequestError as exc:
            raise RuntimeError(
                "Não foi possível conectar ao serviço de pesquisa."
            ) from exc

        if response.status_code in (401, 403):
            raise RuntimeError(
                "O Tavily recusou a autenticação. Verifique a chave API."
            )

        if response.status_code == 429:
            raise RuntimeError(
                "Limite de utilização do Tavily atingido."
            )

        if response.status_code >= 500:
            raise RuntimeError(
                "O serviço Tavily está temporariamente indisponível."
            )

        if response.status_code >= 400:
            raise RuntimeError(
                f"O Tavily retornou erro HTTP {response.status_code}."
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                "O Tavily retornou uma resposta JSON inválida."
            ) from exc

        if not isinstance(data, dict):
            raise RuntimeError(
                "Formato de resposta inválido do Tavily."
            )

        raw_results = data.get("results")

        if not isinstance(raw_results, list):
            raise RuntimeError(
                "A resposta do Tavily não contém uma lista de resultados."
            )

        results = []

        for item in raw_results:
            if not isinstance(item, dict):
                continue

            title = item.get("title")
            url = item.get("url")
            snippet = item.get("content", "")

            if not isinstance(title, str):
                continue

            if not isinstance(url, str):
                continue

            if not isinstance(snippet, str):
                snippet = ""

            try:
                result = SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet[:MAX_SNIPPET_LENGTH],
                    provider=self.name,
                )
            except ValueError:
                continue

            results.append(result)

            if len(results) >= limit:
                break

        return results


class SearchEngine:
    """Coordena provedores, valida resultados e remove duplicidades."""

    def __init__(
        self,
        providers: Sequence[SearchProvider],
    ):
        self.providers = tuple(providers)

    def search(
        self,
        query: str,
        limit: int = 10,
    ) -> list[SearchResult]:

        if not isinstance(query, str):
            raise ValueError("A pesquisa precisa ser um texto.")

        query = " ".join(query.split())

        if len(query) < 3:
            raise ValueError("A pesquisa precisa ter pelo menos 3 caracteres.")

        if len(query) > MAX_QUERY_LENGTH:
            raise ValueError("A pesquisa ultrapassou o limite permitido.")

        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("O limite de resultados precisa ser inteiro.")

        if not 1 <= limit <= MAX_RESULTS:
            raise ValueError(
                f"O limite deve estar entre 1 e {MAX_RESULTS}."
            )

        if not self.providers:
            raise RuntimeError(
                "Nenhum provedor de pesquisa foi configurado."
            )

        results = []
        seen_urls = set()

        for provider in self.providers:
            remaining = limit - len(results)

            if remaining <= 0:
                break

            provider_results = provider.search(
                query,
                limit=min(remaining, TAVILY_MAX_RESULTS),
            )

            for result in provider_results:
                if not isinstance(result, SearchResult):
                    raise TypeError(
                        "O provedor retornou um resultado inválido."
                    )

                if result.url in seen_urls:
                    continue

                seen_urls.add(result.url)

                if not result.provider:
                    result = SearchResult(
                        title=result.title,
                        url=result.url,
                        snippet=result.snippet,
                        provider=provider.name,
                    )

                results.append(result)

                if len(results) >= limit:
                    break

        return results


def build_company_queries(company_name: str) -> list[str]:
    """Gera pesquisas para localizar páginas oficiais de carreiras."""

    if not isinstance(company_name, str):
        raise ValueError("O nome da empresa precisa ser um texto.")

    company_name = " ".join(company_name.split())

    if len(company_name) < 2:
        raise ValueError("Informe um nome de empresa válido.")

    if len(company_name) > 150:
        raise ValueError("Nome da empresa muito longo.")

    return [
        f'"{company_name}" trabalhe conosco',
        f'"{company_name}" carreiras vagas',
        f'"{company_name}" oportunidades de emprego',
        f'"{company_name}" página oficial de carreiras',
    ]
    
