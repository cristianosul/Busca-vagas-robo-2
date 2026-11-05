from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class CandidateIn(BaseModel):
    """Dados recebidos de uma fonte; não aceita declarações de verificação do cliente."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(min_length=3, max_length=250)
    company: str = Field(min_length=2, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    mode: Literal["Presencial", "Híbrido", "Remoto", "Não informado"] = "Não informado"
    employment_type: str | None = Field(default=None, max_length=100)
    source_name: str = Field(min_length=2, max_length=150)
    source_url: HttpUrl
    original_job_url: HttpUrl
    application_url: HttpUrl | None = None
    date_posted: datetime | None = None
    valid_through: datetime | None = None


class CandidateEvidence(BaseModel):
    """Evidências internas do pipeline. Nunca é recebida na rota pública."""

    company_verified: bool = False
    original_page_verified: bool = False
    application_link_verified: bool = False
    job_content_verified: bool = False
    date_verified: bool = False
    rejected_reason: str | None = Field(default=None, max_length=500)
