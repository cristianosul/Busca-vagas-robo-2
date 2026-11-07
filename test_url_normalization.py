
import pytest

import app.repository as repository
from app.models import CandidateIn


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DATABASE_URL", "")
    monkeypatch.setattr(
        repository,
        "DB_PATH",
        tmp_path / "test_urls.db",
    )
    repository.init_db()


def make_candidate(url):
    return CandidateIn(
        title="Auxiliar administrativo",
        company="Empresa de Teste",
        location="Brasil",
        mode="Não informado",
        source_name="Teste interno",
        source_url="https://example.com/careers",
        original_job_url=url,
        application_url=None,
    )


def test_removes_tracking_parameters_and_fragment():
    url = (
        "https://example.com/jobs/123"
        "?utm_source=google&fbclid=abc&lang=pt#apply"
    )

    normalized = repository.normalize_job_url(url)

    assert normalized == "https://example.com/jobs/123?lang=pt"


def test_preserves_legitimate_query_parameters():
    url = "https://example.com/jobs?id=123&department=finance"

    normalized = repository.normalize_job_url(url)

    assert normalized == (
        "https://example.com/jobs?department=finance&id=123"
    )


def test_different_job_ids_remain_different():
    first = repository.normalize_job_url(
        "https://example.com/jobs?id=123"
    )
    second = repository.normalize_job_url(
        "https://example.com/jobs?id=456"
    )

    assert first != second


def test_equivalent_urls_are_detected_as_duplicates(isolated_db):
    first_candidate = make_candidate(
        "https://example.com/jobs/123?utm_source=google#apply"
    )
    second_candidate = make_candidate(
        "https://EXAMPLE.com/jobs/123/"
    )

    first = repository.save_candidate(first_candidate)
    second = repository.save_candidate(second_candidate)

    assert first["status"] == "review"
    assert second["status"] == "duplicate"
    assert second["id"] == first["id"]
