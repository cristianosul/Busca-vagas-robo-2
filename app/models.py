
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
)


SourceType = Literal[
    "company",
    "employment_agency",
    "recruitment_consultancy",
    "temporary_work_agency",
    "headhunter",
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

EvidenceCriterion = Literal[
    "company_verified",
    "original_page_verified",
    "application_link_verified",
    "job_content_verified",
    "date_verified",
]

EvidenceResult = Literal[
    "verified",
    "not_verified",
    "inconclusive",
]


class CandidateIn(BaseModel):
    """Dados recebidos de uma fonte; não aceita declarações de verificação do cliente."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
    )

    title: str = Field(min_length=3, max_length=250)
    company: str = Field(min_length=2, max_length=200)

    advertiser_company: str | None = Field(
        default=None,
        max_length=200,
    )
    hiring_company: str | None = Field(
        default=None,
        max_length=200,
    )
    intermediary_name: str | None = Field(
        default=None,
        max_length=200,
    )

    location: str | None = Field(default=None, max_length=200)

    mode: Literal[
        "Presencial",
        "Híbrido",
        "Remoto",
        "Não informado",
    ] = "Não informado"

    employment_type: str | None = Field(
        default=None,
        max_length=100,
    )

    source_name: str = Field(min_length=2, max_length=150)
    source_url: HttpUrl
    original_job_url: HttpUrl
    application_url: HttpUrl | None = None

    date_posted: datetime | None = None
    valid_through: datetime | None = None


class EvidenceRecord(BaseModel):
    """Registro individual de procedência de uma verificação interna."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
    )

    criterion: EvidenceCriterion
    evidence_url: HttpUrl
    method: str = Field(min_length=2, max_length=100)
    checked_at: datetime
    result: EvidenceResult
    details: str | None = Field(
        default=None,
        max_length=1000,
    )


class CandidateEvidence(BaseModel):
    """Evidências internas do pipeline. Nunca é recebida na rota pública."""

    model_config = ConfigDict(extra="forbid")

    company_verified: bool = False
    original_page_verified: bool = False
    application_link_verified: bool = False
    job_content_verified: bool = False
    date_verified: bool = False

    evidence_records: list[EvidenceRecord] = Field(
        default_factory=list,
    )

    rejected_reason: str | None = Field(
        default=None,
        max_length=500,
    )


class SourceCreate(BaseModel):
    """Dados permitidos para cadastrar uma fonte de oportunidades."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
    )

    source_id: str = Field(
        min_length=2,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )

    name: str = Field(min_length=2, max_length=150)
    source_type: SourceType
    base_url: HttpUrl

    official_site_url: HttpUrl | None = None
    authorization_evidence_url: HttpUrl | None = None

    authorization: AuthorizationType
    enabled: bool = False

    notes: str | None = Field(
        default=None,
        max_length=1000,
    )

    @field_validator(
        "base_url",
        "official_site_url",
        "authorization_evidence_url",
    )
    @classmethod
    def require_https(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is None:
            return value

        parsed = urlparse(str(value))

        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError(
                "A fonte precisa utilizar uma URL HTTPS válida."
            )

        return value


class SourceStatusUpdate(BaseModel):
    """Permite alterar somente o estado de ativação de uma fonte."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    
