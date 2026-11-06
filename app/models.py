
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


class CandidateIn(BaseModel):
    """Dados recebidos de uma fonte; não aceita declarações de verificação do cliente."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
    )

    title: str = Field(min_length=3, max_length=250)
    company: str = Field(min_length=2, max_length=200)
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


class CandidateEvidence(BaseModel):
    """Evidências internas do pipeline. Nunca é recebida na rota pública."""

    model_config = ConfigDict(extra="forbid")

    company_verified: bool = False
    original_page_verified: bool = False
    application_link_verified: bool = False
    job_content_verified: bool = False
    date_verified: bool = False
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
    authorization: AuthorizationType
    enabled: bool = False
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("base_url")
    @classmethod
    def require_https(cls, value: HttpUrl) -> HttpUrl:
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
