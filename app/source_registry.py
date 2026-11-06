
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse


SourceType = Literal[
    "company",
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "gupy",
    "job_portal",
    "regional",
    "municipal",
    "sitemap",
    "structured_data",
]

AuthorizationType = Literal[
    "official_public_api",
    "official_public_page",
    "public_sitemap",
    "public_structured_data",
    "written_permission",
]


@dataclass(frozen=True)
class Source:
    source_id: str
    name: str
    source_type: SourceType
    base_url: str
    authorization: AuthorizationType
    enabled: bool = False
    notes: str | None = None


# Nenhuma fonte é ativada nesta etapa.
# Cada fonte deverá ser cadastrada e avaliada antes da pesquisa.
SOURCES: tuple[Source, ...] = ()


def validate_source(source: Source) -> None:
    if not source.source_id.strip():
        raise ValueError("A fonte precisa de um identificador.")

    if not source.name.strip():
        raise ValueError("A fonte precisa de um nome.")

    parsed = urlparse(source.base_url)

    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("A fonte precisa utilizar uma URL HTTPS válida.")

    if source.authorization not in (
        "official_public_api",
        "official_public_page",
        "public_sitemap",
        "public_structured_data",
        "written_permission",
    ):
        raise ValueError("Tipo de autorização não reconhecido.")


def get_sources(enabled_only: bool = False) -> list[Source]:
    sources = list(SOURCES)

    if enabled_only:
        return [
            source
            for source in sources
            if source.enabled
        ]

    return sources


def get_source(source_id: str) -> Source | None:
    for source in SOURCES:
        if source.source_id == source_id:
            return source

    return None


def register_source(source: Source) -> tuple[Source, ...]:
    validate_source(source)

    if any(item.source_id == source.source_id for item in SOURCES):
        raise ValueError("Já existe uma fonte com esse identificador.")

    return SOURCES + (source,)
