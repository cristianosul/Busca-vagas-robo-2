
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


SOURCES: list[Source] = []

VALID_SOURCE_TYPES = {
    "company", "greenhouse", "lever", "ashby",
    "smartrecruiters", "gupy", "job_portal",
    "regional", "municipal", "sitemap",
    "structured_data",
}

VALID_AUTHORIZATIONS = {
    "official_public_api",
    "official_public_page",
    "public_sitemap",
    "public_structured_data",
    "written_permission",
}


def validate_source(source: Source) -> None:
    if not source.source_id.strip():
        raise ValueError("A fonte precisa de um identificador.")

    if not source.name.strip():
        raise ValueError("A fonte precisa de um nome.")

    if source.source_type not in VALID_SOURCE_TYPES:
        raise ValueError("Tipo de fonte não reconhecido.")

    if source.authorization not in VALID_AUTHORIZATIONS:
        raise ValueError("Tipo de autorização não reconhecido.")

    parsed = urlparse(source.base_url)

    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("A fonte precisa utilizar uma URL HTTPS válida.")


def get_sources(enabled_only: bool = False) -> list[Source]:
    if enabled_only:
        return [source for source in SOURCES if source.enabled]

    return list(SOURCES)


def get_source(source_id: str) -> Source | None:
    for source in SOURCES:
        if source.source_id == source_id:
            return source

    return None


def register_source(source: Source) -> Source:
    validate_source(source)

    if get_source(source.source_id) is not None:
        raise ValueError("Já existe uma fonte com esse identificador.")

    SOURCES.append(source)
    return source
