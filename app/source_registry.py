
from dataclasses import dataclass
from typing import Literal

from app.models import SourceCreate
from app.repository import (
    get_source_record,
    list_sources,
    save_source,
)


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


def _to_model(source: Source) -> SourceCreate:
    return SourceCreate(
        source_id=source.source_id,
        name=source.name,
        source_type=source.source_type,
        base_url=source.base_url,
        authorization=source.authorization,
        enabled=source.enabled,
        notes=source.notes,
    )


def _to_source(record: dict) -> Source:
    return Source(
        source_id=record["source_id"],
        name=record["name"],
        source_type=record["source_type"],
        base_url=record["base_url"],
        authorization=record["authorization"],
        enabled=bool(record["enabled"]),
        notes=record.get("notes"),
    )


def validate_source(source: Source) -> None:
    _to_model(source)


def get_sources(enabled_only: bool = False) -> list[Source]:
    records = list_sources(enabled_only=enabled_only)
    return [_to_source(record) for record in records]


def get_source(source_id: str) -> Source | None:
    record = get_source_record(source_id)

    if record is None:
        return None

    return _to_source(record)


def register_source(source: Source) -> Source:
    model = _to_model(source)
    save_source(model)

    return get_source(source.source_id) or source
