from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
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

CandidateMode = Literal[
    "Presencial",
    "Híbrido",
    "Remoto",
    "Não informado",
]


class ExtractedJobData(BaseModel):
    """Dados extraídos de uma página, ainda não verificados nem aprovados.

    Este modelo aceita empresa desconhecida e datas ainda em formato textual.
    Ele é intermediário: não substitui CandidateIn e não atribui pontuação.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
    )

    title: str | None = Field(default=None, min_length=1, max_length=250)
    company: str | None = Field(default=None, max_length=200)
    company_hint: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=50_000)
    requirements: str | None = Field(default=None, max_length=50_000)
    location: str | None = Field(default=None, max_length=500)
    mode: CandidateMode = "Não informado"
    employment_type: str | None = Field(default=None, max_length=200)
    date_posted: str | None = Field(default=None, max_length=100)
    valid_through: str | None = Field(default=None, max_length=100)
    application_urls: list[HttpUrl] = Field(default_factory=list, max_length=20)
    page_url: HttpUrl
    extraction_method: str = Field(min_length=2, max_length=80)
    structured_data_found: bool = False
    missing_fields: list[str] = Field(default_factory=list, max_length=30)
    warnings: list[str] = Field(default_factory=list, max_length=50)
    canonical_url: HttpUrl | None = None
    identifier: str | None = Field(default=None, max_length=250)
    job_url: HttpUrl | None = None

    @field_validator("application_urls")
    @classmethod
    def deduplicate_application_urls(cls, values: list[HttpUrl]) -> list[HttpUrl]:
        """Preserva a ordem e elimina URLs de candidatura repetidas."""
        result: list[HttpUrl] = []
        seen: set[str] = set()
        for value in values:
            normalized = str(value)
            if normalized not in seen:
                seen.add(normalized)
                result.append(value)
        return result


class CandidateIn(BaseModel):
    """Dados recebidos de uma fonte; não aceita declarações de verificação do cliente.

    A empresa continua obrigatória neste modelo porque a tabela candidates
    existente exige company NOT NULL. Extrações sem empresa devem permanecer
    em ExtractedJobData até que a identidade seja determinada.
    """

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
    company_hint: str | None = Field(default=None, max_length=200)

    location: str | None = Field(default=None, max_length=500)

    mode: CandidateMode = "Não informado"

    employment_type: str | None = Field(
        default=None,
        max_length=200,
    )

    description: str | None = Field(default=None, max_length=50_000)
    requirements: str | None = Field(default=None, max_length=50_000)

    source_name: str = Field(min_length=2, max_length=150)
    source_url: HttpUrl
    original_job_url: HttpUrl
    application_url: HttpUrl | None = None
    application_urls: list[HttpUrl] = Field(default_factory=list, max_length=20)
    canonical_url: HttpUrl | None = None
    identifier: str | None = Field(default=None, max_length=250)
    job_url: HttpUrl | None = None

    extraction_method: str | None = Field(default=None, max_length=80)
    structured_data_found: bool = False
    missing_fields: list[str] = Field(default_factory=list, max_length=30)
    extraction_warnings: list[str] = Field(default_factory=list, max_length=50)

    date_posted: datetime | None = None
    valid_through: datetime | None = None

    @field_validator("application_urls")
    @classmethod
    def deduplicate_application_urls(cls, values: list[HttpUrl]) -> list[HttpUrl]:
        result: list[HttpUrl] = []
        seen: set[str] = set()
        for value in values:
            normalized = str(value)
            if normalized not in seen:
                seen.add(normalized)
                result.append(value)
        return result

    @model_validator(mode="after")
    def synchronize_application_urls(self):
        """Mantém compatibilidade com application_url e preserva alternativas."""
        urls = list(self.application_urls)
        primary = str(self.application_url) if self.application_url else None
        url_strings = {str(url) for url in urls}

        if primary and primary not in url_strings:
            urls.insert(0, self.application_url)
        elif not primary and urls:
            self.application_url = urls[0]

        self.application_urls = urls
        return self


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
        max_length=250,
    )

    rejected_reason: str | None = Field(
        default=None,
        max_length=500,
    )

    @staticmethod
    def _timestamp(value: datetime) -> float:
        """Compara datas ingênuas como UTC para evitar mistura naive/aware."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    @model_validator(mode="after")
    def validate_verified_evidence(self):
        """Exige evidência positiva mais recente para cada flag verdadeira.

        Um registro negativo ou inconclusivo posterior invalida uma confirmação
        positiva anterior. Empates no horário só permitem True se todos os
        registros mais recentes forem verified.
        """
        criteria = (
            "company_verified",
            "original_page_verified",
            "application_link_verified",
            "job_content_verified",
            "date_verified",
        )

        for criterion in criteria:
            if not getattr(self, criterion):
                continue

            matching = [
                record
                for record in self.evidence_records
                if record.criterion == criterion
            ]
            if not matching:
                raise ValueError(
                    f"O critério '{criterion}' não pode ser verdadeiro "
                    "sem um registro de evidência correspondente."
                )

            latest_timestamp = max(
                self._timestamp(record.checked_at)
                for record in matching
            )
            latest_records = [
                record
                for record in matching
                if self._timestamp(record.checked_at) == latest_timestamp
            ]

            if not all(record.result == "verified" for record in latest_records):
                raise ValueError(
                    f"O critério '{criterion}' não pode permanecer verdadeiro: "
                    "a evidência mais recente não confirma a verificação."
                )

        return self


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
