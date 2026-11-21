"""Testes isolados do worker de extração do Busca Vagas — Robô 2."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import extraction_worker as worker
from app import repository as repo


BASE_URL = "https://empresa.example/vagas/123"
JOB_HTML = '''
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "JobPosting",
  "title": "Auxiliar administrativo",
  "description": "Atividades administrativas e organização de documentos.",
  "hiringOrganization": {"@type": "Organization", "name": "Empresa Exemplo"},
  "jobLocation": {"@type": "Place", "address": {"addressLocality": "Caxias do Sul", "addressRegion": "RS", "addressCountry": "BR"}},
  "url": "https://empresa.example/vagas/123"
}
</script>
</head><body><h1>Auxiliar administrativo</h1></body></html>
'''


@pytest.fixture
def isolated_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "DATABASE_URL", "")
    monkeypatch.setattr(repo, "DB_PATH", tmp_path / "worker-test.db")
    repo.init_db()
    return repo


def add_discovery(repository, *, url=BASE_URL, result=None):
    return repository.save_discovery_result(
        municipality_code="4305108",
        municipality_name="Caxias do Sul",
        state="RS",
        category="job_search",
        query="auxiliar administrativo",
        normalized_url=url,
        result=result or {"title": "Vaga encontrada", "url": url, "provider": "Página oficial"},
    )


def enqueue_and_claim(repository, *, max_attempts=5):
    discovery = add_discovery(repository)
    repository.enqueue_extraction(discovery["id"], max_attempts=max_attempts)
    return repository.claim_next_extraction(), discovery


def fake_fetch(html=JOB_HTML, final_url=BASE_URL):
    return SimpleNamespace(html=html, final_url=final_url)


def test_process_claimed_extraction_saves_intermediate_and_completes(
    isolated_repository, monkeypatch
):
    claimed, discovery = enqueue_and_claim(isolated_repository)
    monkeypatch.setattr(worker, "fetch_job_page", lambda url: fake_fetch())

    outcome = worker.process_claimed_extraction(claimed)

    assert outcome["status"] == "completed"
    assert outcome["queue_id"] == claimed["id"]
    assert outcome["extracted_count"] == 1
    assert len(outcome["extraction_ids"]) == 1
    saved = isolated_repository.list_extracted_jobs()
    assert len(saved) == 1
    assert saved[0]["discovery_result_id"] == discovery["id"]
    assert saved[0]["source_name"] == "Página oficial"
    assert saved[0]["status"] == "complete"
    assert isolated_repository.list_candidates() == []
    assert isolated_repository.list_extraction_queue(status="completed")[0]["id"] == claimed["id"]


def test_process_claimed_extraction_records_fetch_failure_for_retry(
    isolated_repository, monkeypatch
):
    claimed, _ = enqueue_and_claim(isolated_repository)

    def fail_fetch(_url):
        raise RuntimeError("falha simulada de leitura")

    monkeypatch.setattr(worker, "fetch_job_page", fail_fetch)
    outcome = worker.process_claimed_extraction(claimed, retry_delay_seconds=30)

    assert outcome["status"] == "failed"
    assert "falha simulada" in outcome["error"]
    queued = isolated_repository.list_extraction_queue()[0]
    assert queued["status"] == "pending"
    assert queued["attempts"] == 1
    assert queued["last_error"] == "falha simulada de leitura"
    assert isolated_repository.list_extracted_jobs() == []


def test_process_claimed_extraction_marks_terminal_failure_after_max_attempts(
    isolated_repository, monkeypatch
):
    claimed, _ = enqueue_and_claim(isolated_repository, max_attempts=1)
    monkeypatch.setattr(
        worker,
        "fetch_job_page",
        lambda _url: (_ for _ in ()).throw(RuntimeError("erro definitivo simulado")),
    )

    outcome = worker.process_claimed_extraction(claimed)

    assert outcome["status"] == "failed"
    assert isolated_repository.list_extraction_queue(status="failed")[0]["id"] == claimed["id"]


def test_old_worker_cannot_complete_task_after_lease_is_lost(
    isolated_repository, monkeypatch
):
    claimed, _ = enqueue_and_claim(isolated_repository)
    monkeypatch.setattr(worker, "fetch_job_page", lambda _url: fake_fetch())
    monkeypatch.setattr(worker, "mark_extraction_completed", lambda *_args: None)
    failed_calls = []

    def mark_failed(queue_id, token, error, *, retry_delay_seconds):
        failed_calls.append((queue_id, token, error, retry_delay_seconds))
        return None

    monkeypatch.setattr(worker, "mark_extraction_failed", mark_failed)
    outcome = worker.process_claimed_extraction(claimed)

    assert outcome["status"] == "failed"
    assert "Lease perdido" in outcome["error"]
    assert len(failed_calls) == 1
    assert failed_calls[0][0:2] == (claimed["id"], claimed["lease_token"])
    # A persistência intermediária pode ter ocorrido antes da perda do lease;
    # o worker antigo não conclui nem altera a reserva recuperada.
    assert len(isolated_repository.list_extracted_jobs()) == 1
    assert isolated_repository.list_extraction_queue(status="running")[0]["id"] == claimed["id"]


def test_run_worker_processes_only_requested_batch_and_initializes(
    isolated_repository, monkeypatch
):
    add_discovery(isolated_repository, url="https://empresa.example/vagas/1")
    add_discovery(
        isolated_repository,
        url="https://empresa.example/vagas/2",
        result={"title": "Outra vaga", "url": "https://empresa.example/vagas/2"},
    )
    monkeypatch.setattr(worker, "fetch_job_page", lambda _url: fake_fetch())
    initialized = []
    monkeypatch.setattr(worker, "init_db", lambda: initialized.append(True))

    summary = worker.run_worker(batch_size=1, enqueue_limit=10)

    assert initialized == [True]
    assert summary.enqueued == 2
    assert summary.claimed == 1
    assert summary.completed == 1
    assert summary.failed == 0
    assert summary.extracted == 1
    assert len(isolated_repository.list_extraction_queue(status="pending")) == 1


def test_run_worker_recovers_expired_lease_before_claiming(
    isolated_repository, monkeypatch
):
    discovery = add_discovery(isolated_repository)
    isolated_repository.enqueue_extraction(discovery["id"])
    old_claim = isolated_repository.claim_next_extraction()
    with repo.db_session(immediate=True) as conn:
        repo.execute(
            conn,
            "UPDATE extraction_queue SET lease_expires_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(), old_claim["id"]),
        )
    monkeypatch.setattr(worker, "fetch_job_page", lambda _url: fake_fetch())

    summary = worker.run_worker(batch_size=1, enqueue_limit=10, stale_after_seconds=0)

    assert summary.recovered == 1
    assert summary.claimed == 1
    assert summary.completed == 1
    queue = isolated_repository.list_extraction_queue(status="completed")
    assert queue[0]["attempts"] == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"batch_size": 0},
        {"batch_size": 101},
        {"batch_size": True},
        {"enqueue_limit": 0},
        {"enqueue_limit": 5001},
        {"max_attempts": 0},
        {"lease_seconds": 0},
        {"retry_delay_seconds": 0},
        {"stale_after_seconds": -1},
        {"stale_after_seconds": 7 * 24 * 3600 + 1},
    ],
)
def test_run_worker_rejects_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        worker.run_worker(**kwargs)


def test_process_claimed_extraction_rejects_invalid_lease_payload():
    with pytest.raises(ValueError, match="ID de fila"):
        worker.process_claimed_extraction({"id": 0, "lease_token": "token"})
    with pytest.raises(ValueError, match="token de lease"):
        worker.process_claimed_extraction({"id": 1, "lease_token": ""})
