
from collections.abc import Sequence
from dataclasses import dataclass

from app.company_discovery import (
    CareerPageCandidate,
    discover_career_pages,
)
from app.search_engine import (
    MAX_QUERY_LENGTH,
    MAX_RESULTS,
    SearchEngine,
    SearchProviderError,
    SearchResult,
    build_company_queries,
    normalize_search_url,
)


MAX_NATIONAL_QUERIES = 60

# Categorias independentes de pesquisa nacional.
NATIONAL_SEARCHES = {
    "companies": (
        "empresas contratando no Brasil página oficial carreiras vagas",
        "empresas brasileiras trabalhe conosco oportunidades de emprego",
    ),
    "agencies": (
        "agências de emprego recrutamento e seleção Brasil vagas",
        "consultorias de recrutamento seleção oportunidades Brasil",
    ),
    "temporary_work": (
        "empresas de trabalho temporário Brasil oportunidades de emprego",
        "agências de trabalho temporário vagas Brasil site oficial",
    ),
    "headhunters": (
        "consultorias headhunter recrutamento Brasil oportunidades",
        "empresas de executive search recrutamento Brasil",
    ),
    "ats": (
        "vagas Brasil Gupy Greenhouse Lever Ashby carreiras",
        "vagas de emprego Brasil Workday SmartRecruiters carreiras",
    ),
    "regional_public_sources": (
        "SINE vagas de emprego fontes oficiais estaduais municipais Brasil",
        "secretarias do trabalho vagas de emprego estados municípios Brasil",
    ),
}


@dataclass(frozen=True)
class DiscoveryQueryError:
    """Registra uma falha ocorrida em uma consulta."""

    query: str
    message: str


@dataclass(frozen=True)
class DiscoveryMatch:
    """Registra a origem de um resultado de pesquisa."""

    category: str
    query: str
    normalized_url: str
    result: SearchResult


@dataclass(frozen=True)
class NationalDiscoveryReport:
    """Resultado organizado de uma operação de pesquisa."""

    queries: tuple[str, ...]
    results: tuple[SearchResult, ...]
    errors: tuple[DiscoveryQueryError, ...]
    matches: tuple[DiscoveryMatch, ...] = ()


class NationalDiscoveryCoordinator:
    """
    Coordena pesquisas nacionais e descoberta de páginas de carreira.

    Não cadastra fontes, não armazena oportunidades e não publica vagas.
    """

    def __init__(self, search_engine: SearchEngine):
        if not isinstance(search_engine, SearchEngine):
            raise ValueError(
                "É necessário fornecer uma instância válida de SearchEngine."
            )

        self.search_engine = search_engine

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("O limite precisa ser um número inteiro.")

        if not 1 <= limit <= MAX_RESULTS:
            raise ValueError(
                f"O limite precisa estar entre 1 e {MAX_RESULTS}."
            )

    @staticmethod
    def build_national_queries(
        categories: Sequence[str] | None = None,
    ) -> list[str]:
        """
        Monta consultas para as categorias nacionais disponíveis.

        Quando nenhuma categoria é informada, utiliza todas.
        """

        if categories is None:
            selected_categories = list(NATIONAL_SEARCHES)

        else:
            if isinstance(categories, (str, bytes)):
                raise ValueError(
                    "As categorias precisam ser fornecidas em uma lista."
                )

            try:
                selected_categories = list(categories)
            except TypeError as exc:
                raise ValueError(
                    "Lista de categorias inválida."
                ) from exc

            if not selected_categories:
                raise ValueError(
                    "Informe pelo menos uma categoria de pesquisa."
                )

        queries = []
        seen_categories = set()

        for category in selected_categories:
            if not isinstance(category, str):
                raise ValueError(
                    "Cada categoria precisa ser um texto."
                )

            category = category.strip().lower()

            if not category:
                raise ValueError(
                    "Não são permitidas categorias vazias."
                )

            if category not in NATIONAL_SEARCHES:
                raise ValueError(
                    f"Categoria de pesquisa não reconhecida: {category}."
                )

            if category in seen_categories:
                continue

            seen_categories.add(category)
            queries.extend(NATIONAL_SEARCHES[category])

        if not queries:
            raise ValueError(
                "Nenhuma consulta de pesquisa foi gerada."
            )

        if len(queries) > MAX_NATIONAL_QUERIES:
            raise ValueError(
                "O número de consultas ultrapassou o limite permitido."
            )

        for query in queries:
            if len(query) > MAX_QUERY_LENGTH:
                raise ValueError(
                    "Uma consulta ultrapassou o tamanho máximo permitido."
                )

        return queries

    def _run_queries(
        self,
        query_items: Sequence[tuple[str, str]],
        limit_per_query: int,
    ) -> NationalDiscoveryReport:
        """Executa consultas e preserva a origem de cada resultado."""

        self._validate_limit(limit_per_query)

        results = []
        matches = []
        errors = []
        seen_urls = set()

        for category, query in query_items:
            try:
                search_results = self.search_engine.search(
                    query,
                    limit=limit_per_query,
                )

            except SearchProviderError as exc:
                errors.append(
                    DiscoveryQueryError(
                        query=query,
                        message=str(exc),
                    )
                )
                continue

            for result in search_results:
                normalized_url = normalize_search_url(result.url)

                matches.append(
                    DiscoveryMatch(
                        category=category,
                        query=query,
                        normalized_url=normalized_url,
                        result=result,
                    )
                )

                if normalized_url in seen_urls:
                    continue

                seen_urls.add(normalized_url)
                results.append(result)

        return NationalDiscoveryReport(
            queries=tuple(query for _, query in query_items),
            results=tuple(results),
            errors=tuple(errors),
            matches=tuple(matches),
        )

    def search_national(
        self,
        categories: Sequence[str] | None = None,
        limit_per_query: int = 10,
    ) -> NationalDiscoveryReport:
        """
        Executa pesquisas nacionais nas categorias selecionadas.

        Categorias disponíveis:
        companies
        agencies
        temporary_work
        headhunters
        ats
        regional_public_sources
        """

        queries = self.build_national_queries(categories)

        if categories is None:
            selected_categories = list(NATIONAL_SEARCHES)
        else:
            selected_categories = list(
                dict.fromkeys(
                    category.strip().lower()
                    for category in categories
                )
            )

        query_items = [
            (category, query)
            for category in selected_categories
            for query in NATIONAL_SEARCHES[category]
        ]

        return self._run_queries(
            query_items,
            limit_per_query,
        )

    def search_company(
        self,
        company_name: str,
        limit_per_query: int = 10,
    ) -> NationalDiscoveryReport:
        """Pesquisa páginas e oportunidades relacionadas a uma empresa."""

        queries = build_company_queries(company_name)

        query_items = [
            ("company", query)
            for query in queries
        ]

        return self._run_queries(
            query_items,
            limit_per_query,
        )

    @staticmethod
    def inspect_official_site(
        company_name: str,
        official_site_url: str,
    ) -> list[CareerPageCandidate]:
        """
        Examina um site oficial conhecido e procura páginas de carreira.

        A URL recebida é tratada como candidata: a pesquisa não comprova,
        por si só, que o domínio pertence à empresa.
        """

        return discover_career_pages(
            company_name,
            official_site_url,
        )
        
