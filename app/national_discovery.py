import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

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

# O catálogo é versionado junto com o projeto. A leitura local evita depender
# da disponibilidade da API do IBGE durante os testes e a execução do worker.
MUNICIPALITIES_CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "ibge"
    / "municipios.json"
)


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

# Divisão oficial das 27 unidades federativas nas cinco grandes regiões.
REGION_STATES = {
    "region:norte": frozenset({"AC", "AP", "AM", "PA", "RO", "RR", "TO"}),
    "region:nordeste": frozenset({"AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"}),
    "region:centro-oeste": frozenset({"DF", "GO", "MT", "MS"}),
    "region:sudeste": frozenset({"ES", "MG", "RJ", "SP"}),
    "region:sul": frozenset({"PR", "RS", "SC"}),
}

BRAZILIAN_STATE_CODES = frozenset(
    category.removeprefix("state:") for category in STATE_SEARCHES
)

# Os códigos oficiais de UF usados como prefixo dos códigos municipais do IBGE.
UF_IBGE_CODES = {
    "RO": "11",
    "AC": "12",
    "AM": "13",
    "RR": "14",
    "PA": "15",
    "AP": "16",
    "TO": "17",
    "MA": "21",
    "PI": "22",
    "CE": "23",
    "RN": "24",
    "PB": "25",
    "PE": "26",
    "AL": "27",
    "SE": "28",
    "BA": "29",
    "MG": "31",
    "ES": "32",
    "RJ": "33",
    "SP": "35",
    "PR": "41",
    "SC": "42",
    "RS": "43",
    "MS": "50",
    "MT": "51",
    "GO": "52",
    "DF": "53",
}

STATE_NAME_TO_CODE = {
    name.casefold(): code
    for category, name in STATE_SEARCHES.items()
    for code in (category.removeprefix("state:"),)
}


class MunicipalityCatalogError(RuntimeError):
    """Erro ao localizar, ler ou validar o catálogo municipal local do IBGE."""


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


def _normalize_catalog_state(value: object) -> str | None:
    """Converte a sigla ou o nome completo da UF para a sigla oficial."""

    if not isinstance(value, str) or not value.strip():
        return None

    normalized = value.strip()
    upper = normalized.upper()

    if upper in BRAZILIAN_STATE_CODES:
        return upper

    return STATE_NAME_TO_CODE.get(normalized.casefold())


def _normalize_uf_code(value: object) -> str | None:
    """Normaliza codigo_uf sem aceitar booleanos ou valores não numéricos."""

    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        normalized = str(value)
    elif isinstance(value, str) and value.strip().isdigit():
        normalized = value.strip()
    else:
        return None

    return normalized.zfill(2) if len(normalized) <= 2 else None


def _required_catalog_text(item: dict, field: str) -> str | None:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _parse_catalog_municipality(item: object) -> Municipality:
    """Valida os campos do DTB e converte um registro para Municipality."""

    if not isinstance(item, dict):
        raise MunicipalityCatalogError(
            "O catálogo contém um registro municipal inválido."
        )

    code = item.get("codigo_ibge")
    name = _required_catalog_text(item, "municipio")
    state = _normalize_catalog_state(item.get("uf"))
    uf_code = _normalize_uf_code(item.get("codigo_uf"))

    if (
        not isinstance(code, str)
        or len(code) != 7
        or not code.isdigit()
        or name is None
        or state is None
        or uf_code is None
    ):
        raise MunicipalityCatalogError(
            "O catálogo contém dados municipais incompletos ou inválidos."
        )

    expected_uf_code = UF_IBGE_CODES[state]
    if uf_code != expected_uf_code or code[:2] != expected_uf_code:
        raise MunicipalityCatalogError(
            f"O município {name!r} possui código IBGE ou código de UF incompatível."
        )

    # O catálogo DTB contém nove campos por município. Validá-los impede que
    # um arquivo truncado ou parcialmente gerado seja aceito como completo.
    intermediate_code = _required_catalog_text(
        item,
        "codigo_regiao_intermediaria",
    )
    intermediate_name = _required_catalog_text(
        item,
        "regiao_intermediaria",
    )
    immediate_code = _required_catalog_text(
        item,
        "codigo_regiao_imediata",
    )
    immediate_name = _required_catalog_text(
        item,
        "regiao_imediata",
    )
    municipality_code = _required_catalog_text(item, "codigo_municipio")

    if (
        intermediate_code is None
        or not intermediate_code.isdigit()
        or len(intermediate_code) != 4
        or intermediate_name is None
        or immediate_code is None
        or not immediate_code.isdigit()
        or len(immediate_code) != 6
        or immediate_name is None
        or municipality_code is None
        or not municipality_code.isdigit()
        or len(municipality_code) != 5
        or municipality_code != code[-5:]
    ):
        raise MunicipalityCatalogError(
            f"O município {name!r} possui dados territoriais incompletos ou inválidos."
        )

    return Municipality(
        code=code,
        name=name,
        state=state,
    )


@lru_cache(maxsize=1)
def load_brazilian_municipalities() -> tuple[Municipality, ...]:
    """Carrega e valida os municípios do catálogo JSON local do IBGE.

    O arquivo oficial é mantido em data/ibge/municipios.json. O resultado
    validado fica em cache durante a execução do processo.
    """

    try:
        with MUNICIPALITIES_CATALOG_PATH.open(
            "r",
            encoding="utf-8",
        ) as catalog_file:
            payload = json.load(catalog_file)
    except FileNotFoundError as exc:
        raise MunicipalityCatalogError(
            "O catálogo municipal local não foi encontrado em "
            f"{MUNICIPALITIES_CATALOG_PATH}."
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MunicipalityCatalogError(
            "Não foi possível ler o catálogo municipal JSON local."
        ) from exc

    if not isinstance(payload, dict):
        raise MunicipalityCatalogError(
            "A estrutura principal do catálogo municipal é inválida."
        )

    records = payload.get("municipios")
    if not isinstance(records, list) or not records:
        raise MunicipalityCatalogError(
            "O catálogo municipal está vazio ou não contém uma lista válida."
        )

    declared_total = payload.get("total_municipios")
    if (
        isinstance(declared_total, bool)
        or not isinstance(declared_total, int)
        or declared_total != len(records)
    ):
        raise MunicipalityCatalogError(
            "O total declarado de municípios não corresponde aos registros do catálogo."
        )

    municipalities = []
    seen_codes = set()

    for item in records:
        municipality = _parse_catalog_municipality(item)

        if municipality.code in seen_codes:
            raise MunicipalityCatalogError(
                f"O catálogo contém código municipal duplicado: {municipality.code}."
            )

        seen_codes.add(municipality.code)
        municipalities.append(municipality)

    if not municipalities:
        raise MunicipalityCatalogError(
            "Nenhum município válido foi encontrado no catálogo local."
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

    Cada item contém: (categoria, consulta, município, UF).
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
            or state.upper() not in BRAZILIAN_STATE_CODES
        ):
            raise ValueError(
                "A UF do município deve ser uma sigla brasileira válida."
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
