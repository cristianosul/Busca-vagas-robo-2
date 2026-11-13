
from dataclasses import dataclass

from app.models import (
    AuthorizationType,
    SourceCreate,
    SourceType,
)
from app.repository import (
    get_source_record,
    list_sources,
    save_source,
)


@dataclass(frozen=True)
class Source:
    source_id: str
    name: str
    source_type: SourceType
    base_url: str
    authorization: AuthorizationType
    enabled: bool = False
    notes: str | None = None
    official_site_url: str | None = None
    authorization_evidence_url: str | None = None


def _to_model(source: Source) -> SourceCreate:
    return SourceCreate(
        source_id=source.source_id,
        name=source.name,
        source_type=source.source_type,
        base_url=source.base_url,
        authorization=source.authorization,
        enabled=source.enabled,
        notes=source.notes,
        official_site_url=source.official_site_url,
        authorization_evidence_url=source.authorization_evidence_url,
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
        official_site_url=record.get("official_site_url"),
        authorization_evidence_url=record.get(
            "authorization_evidence_url"
        ),
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
    
