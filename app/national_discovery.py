
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

import httpx

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
MAX_MUNICIPALITIES_PER_BATCH = 60

IBGE_MUNICIPALITIES_URL = (
    "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"
)

IBGE_TIMEOUT = 15.0


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


class MunicipalityCatalogError(RuntimeError):
    """Erro ao consultar ou interpretar o cadastro municipal do IBGE."""


@dataclass(frozen=True)
class Municipality:
    code: str
    name: str
    state: str


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
    municipality: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class NationalDiscoveryReport:
    queries: tuple[str, ...]
    results: tuple[SearchResult, ...]
    errors: tuple[DiscoveryQueryError, ...]
    matches: tuple[DiscoveryMatch, ...] = ()


def _extract_municipality_state(item: dict) -> str | None:
    """Obtém a UF considerando a estrutura atual e a estrutura legada do IBGE."""

    immediate = item.get("regiao-imediata") or {}
    intermediate = immediate.get("regiao-intermediaria") or {}
    uf = intermediate.get("UF") or {}

    if uf.get("sigla"):
        return uf["sigla"]

    microregion = item.get("microrregiao") or {}
    mesoregion = microregion.get("mesorregiao") or {}
    uf = mesoregion.get("UF") or {}

    return uf.get("sigla")


@lru_cache(maxsize=1)
def load_brazilian_municipalities() -> tuple[Municipality, ...]:
    """Carrega e valida o cadastro nacional de municípios do IBGE.

    O resultado fica em cache durante a execução do processo.
    """

    try:
        response = httpx.get(
            IBGE_MUNICIPALITIES_URL,
            params={"orderBy": "nome"},
            timeout=IBGE_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": (
                    "BuscaVagasBot/0.1 "
                    "(public Brazilian municipality catalog)"
                )
            },
        )
        response.raise_for_status()

    except httpx.HTTPError as exc:
        raise MunicipalityCatalogError(
            "Não foi possível consultar o cadastro municipal do IBGE."
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise MunicipalityCatalogError(
            "O IBGE retornou uma resposta JSON inválida."
        ) from exc

    if not isinstance(payload, list) or not payload:
        raise MunicipalityCatalogError(
            "O cadastro municipal do IBGE está vazio ou inválido."
        )

    municipalities = []
    seen_codes = set()

    for item in payload:
        if not isinstance(item, dict):
            raise MunicipalityCatalogError(
                "O cadastro contém um município inválido."
            )

        code = item.get("id")
        name = item.get("nome")
        state = _extract_municipality_state(item)

        if (
            isinstance(code, bool)
            or not isinstance(code, int)
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(state, str)
            or len(state) != 2
        ):
            raise MunicipalityCatalogError(
                "O cadastro contém dados municipais incompletos."
            )

        if code in seen_codes:
            continue

        seen_codes.add(code)

        municipalities.append(
            Municipality(
                code=str(code),
                name=name.strip(),
                state=state.upper(),
            )
        )

    if not municipalities:
        raise MunicipalityCatalogError(
            "Nenhum município válido foi encontrado no cadastro do IBGE."
        )

    return tuple(
        sorted(
            municipalities,
            key=lambda municipality: (
                municipality.state,
                municipality.name.casefold(),
                municipality.code,
            ),
        )
    )


def build_municipal_queries(
    batch_index: int = 0,
) -> tuple[tuple[str, str, str, str], ...]:
    """Cria as consultas de um lote municipal.

    Cada item contém:
    (categoria, consulta, município, UF).
    """

    if (
        isinstance(batch_index, bool)
        or not isinstance(batch_index, int)
        or batch_index < 0
    ):
        raise ValueError(
            "batch_index deve ser um inteiro não negativo."
        )

    municipalities = load_brazilian_municipalities()

    start = batch_index * MAX_MUNICIPALITIES_PER_BATCH
    end = start + MAX_MUNICIPALITIES_PER_BATCH

    if start >= len(municipalities):
        raise ValueError(
            "O lote informado está fora do intervalo de municípios."
        )

    batch = municipalities[start:end]
    query_items = []

    for municipality in batch:
        query = (
            "vagas de emprego empresas contratando agências de emprego "
            f"SINE {municipality.name} {municipality.state}"
        )

        if len(query) > MAX_QUERY_LENGTH:
            raise MunicipalityCatalogError(
                "Uma consulta municipal ultrapassou o limite permitido."
            )

        query_items.append(
            (
                f"municipality:{municipality.state}",
                query,
                municipality.name,
                municipality.state,
            )
        )

    return tuple(query_items)


def municipality_batch_count() -> int:
    """Retorna o número de lotes necessários para percorrer os municípios."""

    total = len(load_brazilian_municipalities())

    return (
        total + MAX_MUNICIPALITIES_PER_BATCH - 1
    ) // MAX_MUNICIPALITIES_PER_BATCH


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
        query_items: Sequence[tuple],
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

        for item in query_items:
            if len(item) == 2:
                category, query = item
                municipality = None
                state = None

            elif len(item) == 4:
                category, query, municipality, state = item

            else:
                raise ValueError(
                    "Formato inválido de consulta de pesquisa."
                )

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
                            municipality=municipality,
                            state=state,
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
            queries=tuple(item[1] for item in query_items),
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

    def search_municipalities(
        self,
        batch_index: int = 0,
        limit_per_query: int = 10,
    ):
        """Pesquisa vagas em um lote de municípios brasileiros."""

        self._validate_limit(limit_per_query)

        query_items = build_municipal_queries(
            batch_index=batch_index,
        )

        return self._run_queries(
            query_items,
            limit_per_query,
        )
  
    def search_municipality(
        self,
        municipality: Municipality | dict,
        limit_per_query: int = 10,
    ):
        """Pesquisa oportunidades em um único município brasileiro."""

        self._validate_limit(limit_per_query)

        if isinstance(municipality, Municipality):
            code = municipality.code
            name = municipality.name
            state = municipality.state

        elif isinstance(municipality, dict):
            code = municipality.get("code")
            name = municipality.get("name")
            state = municipality.get("state")

        else:
            raise ValueError(
                "Informe um município válido."
            )

        if (
            not isinstance(code, str)
            or len(code) != 7
            or not code.isdigit()
        ):
            raise ValueError(
                "O código do município deve conter 7 dígitos."
            )

        if (
            not isinstance(name, str)
            or not name.strip()
        ):
            raise ValueError(
                "O nome do município é obrigatório."
            )

        if (
            not isinstance(state, str)
            or len(state) != 2
            or not state.isalpha()
        ):
            raise ValueError(
                "A UF do município deve conter duas letras."
            )

        name = name.strip()
        state = state.upper()

        query = (
            "vagas de emprego empresas contratando agências de emprego "
            f"SINE {name} {state}"
        )

        if len(query) > MAX_QUERY_LENGTH:
            raise ValueError(
                "A consulta municipal ultrapassou o limite permitido."
            )

        query_items = (
            (
                f"municipality:{state}",
                query,
                name,
                state,
            ),
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
