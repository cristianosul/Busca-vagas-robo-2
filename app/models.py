from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field, HttpUrl, ConfigDict


class CandidateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

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

    # Estes campos só devem ser marcados como verdadeiros após verificações reais.
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
    status: str
    reasons: list[str]
    created_at: str
