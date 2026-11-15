
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


REGIONAL_SEARCHES = {
    "region:norte": (
        "fontes oficiais empresas contratando agências de emprego SINE vagas Norte Brasil",
    ),
    "region:nordeste": (
        "fontes oficiais empresas contratando agências de emprego SINE vagas Nordeste Brasil",
    ),
    "region:centro-oeste": (
        "fontes oficiais empresas contratando agências de emprego SINE vagas Centro-Oeste Brasil",
    ),
    "region:sudeste": (
        "fontes oficiais empresas contratando agências de emprego SINE vagas Sudeste Brasil",
    ),
    "region:sul": (
        "fontes oficiais empresas contratando agências de emprego SINE vagas Sul Brasil",
    ),
}


STATE_SEARCHES = {
    "state:AC": "Acre",
    "state:AL": "Alagoas",
    "state:AP": "Amapá",
    "state:AM": "Amazonas",
    "state:BA": "Bahia",
    "state:CE": "Ceará",
    "state:DF": "Distrito Federal",
    "state:ES": "Espírito Santo",
    "state:GO": "Goiás",
    "state:MA": "Maranhão",
    "state:MT": "Mato Grosso",
    "state:MS": "Mato Grosso do Sul",
    "state:MG": "Minas Gerais",
    "state:PA": "Pará",
    "state:PB": "Paraíba",
    "state:PR": "Paraná",
    "state:PE": "Pernambuco",
    "state:PI": "Piauí",
    "state:RJ": "Rio de Janeiro",
    "state:RN": "Rio Grande do Norte",
    "state:RS": "Rio Grande do Sul",
    "state:RO": "Rondônia",
    "state:RR": "Roraima",
    "state:SC": "Santa Catarina",
    "state:SP": "São Paulo",
    "state:SE": "Sergipe",
    "state:TO": "Tocantins",
}


@dataclass(frozen=True)
class DiscoveryQueryError:
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
    queries: tuple[str, ...]
    results: tuple[SearchResult, ...]
    errors: tuple[DiscoveryQueryError, ...]
    matches: tuple[DiscoveryMatch, ...] = ()


class NationalDiscoveryCoordinator:
    def __init__(self, search_engine: SearchEngine):
        if not isinstance(search_engine, SearchEngine):
            raise ValueError("Um SearchEngine válido é obrigatório.")

        self.search_engine = search_engine

    @staticmethod
    def _validate_limit(limit: int):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RESULTS
        ):
            raise ValueError(
                f"O limite deve ser um inteiro entre 1 e {MAX_RESULTS}."
            )

    
    @staticmethod
    def _normalize_categories(categories):
        if categories is None:
            return list(NATIONAL_SEARCHES)

        if isinstance(categories, str):
            raise ValueError("As categorias devem ser uma sequência.")

        try:
            selected = list(categories)
        except TypeError as exc:
            raise ValueError(
                "As categorias devem ser uma sequência."
            ) from exc

        if not selected:
            raise ValueError(
                "Informe pelo menos uma categoria de pesquisa."
            )

        normalized = []

        for category in selected:
            if not isinstance(category, str):
                raise ValueError("Categoria inválida.")

            category = category.strip().lower()

            if category not in NATIONAL_SEARCHES:
                raise ValueError(
                    f"Categoria de pesquisa desconhecida: {category}"
                )

            if category not in normalized:
                normalized.append(category)

        return normalized
        

    @staticmethod
    def _build_query_items(
        categories=None,
        include_geography: bool = False,
    ):
        selected_categories = (
            NationalDiscoveryCoordinator._normalize_categories(categories)
        )

        query_items = []

        for category in selected_categories:
            for query in NATIONAL_SEARCHES[category]:
                query_items.append((category, query))

        if include_geography:
            for category, queries in REGIONAL_SEARCHES.items():
                for query in queries:
                    query_items.append((category, query))

            for category, state_name in STATE_SEARCHES.items():
                query = (
                    "vagas de emprego empresas contratando agências de "
                    f"emprego SINE fontes oficiais {state_name}"
                )
                query_items.append((category, query))

        if len(query_items) > MAX_NATIONAL_QUERIES:
            raise ValueError(
                "O número de consultas ultrapassa o limite nacional."
            )

        for _, query in query_items:
            if (
                not isinstance(query, str)
                or not query.strip()
                or len(query) > MAX_QUERY_LENGTH
            ):
                raise ValueError("Consulta de pesquisa inválida.")

        return query_items

    @staticmethod
    def build_national_queries(
        categories=None,
        include_geography: bool = False,
    ):
        query_items = NationalDiscoveryCoordinator._build_query_items(
            categories=categories,
            include_geography=include_geography,
        )

        return [query for _, query in query_items]

    def _run_queries(
        self,
        query_items: Sequence[tuple[str, str]],
        limit_per_query: int,
    ):
        self._validate_limit(limit_per_query)

        if len(query_items) > MAX_NATIONAL_QUERIES:
            raise ValueError(
                "O número de consultas ultrapassa o limite nacional."
            )

        results = []
        errors = []
        matches = []
        seen_urls = set()

        for category, query in query_items:
            try:
                found = self.search_engine.search(
                    query,
                    limit=limit_per_query,
                )

                for result in found:
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

            except SearchProviderError as exc:
                errors.append(
                    DiscoveryQueryError(
                        query=query,
                        message=str(exc),
                    )
                )

        return NationalDiscoveryReport(
            queries=tuple(query for _, query in query_items),
            results=tuple(results),
            errors=tuple(errors),
            matches=tuple(matches),
        )

    def search_national(
        self,
        categories=None,
        limit_per_query: int = 10,
        include_geography=None,
    ):
        self._validate_limit(limit_per_query)

        if include_geography is None:
            include_geography = categories is None

        if not isinstance(include_geography, bool):
            raise ValueError(
                "include_geography deve ser verdadeiro ou falso."
            )

        query_items = self._build_query_items(
            categories=categories,
            include_geography=include_geography,
        )

        return self._run_queries(
            query_items,
            limit_per_query,
        )

    def search_company(
        self,
        company_name: str,
        limit_per_query: int = 10,
    ):
        self._validate_limit(limit_per_query)

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
    ) -> Sequence[CareerPageCandidate]:
        return discover_career_pages(
            company_name,
            official_site_url,
        )
