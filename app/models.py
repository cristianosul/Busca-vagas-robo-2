
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, ConfigDict, model_validator


class CandidateIn(BaseModel):
    """Dados recebidos pelo Robô 2, ainda sem validação de confiança."""

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

    employment_type: str | None = Field(default=None, max_length=100)

    source_name: str = Field(min_length=2, max_length=150)
    source_url: HttpUrl
    original_job_url: HttpUrl
    application_url: HttpUrl | None = None

    date_posted: datetime | None = None
    valid_through: datetime | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        if self.date_posted and self.valid_through:
            posted = self.date_posted
            expiry = self.valid_through

            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)

            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)

            if expiry < posted:
                raise ValueError(
                    "A validade da vaga não pode ser anterior à publicação."
                )

        return self


class CandidateVerification(BaseModel):
    """Evidências internas produzidas pelos verificadores do Robô 2."""

    company_verified: bool = False
    original_page_verified: bool = False
    application_link_verified: bool = False
    job_content_verified: bool = False
    date_verified: bool = False


class CandidateOut(BaseModel):
    id: int
    title: str
    company: str
    location: str | None
    mode: str
    source_name: str
    original_job_url: str
    application_url: str | None
    score: int
    status: Literal["approved", "review", "rejected"]
    reasons: list[str]
    created_at: str
