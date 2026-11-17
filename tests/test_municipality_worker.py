from threading import Event, Thread
from types import SimpleNamespace

import pytest

import app.municipality_worker as worker


MUNICIPALITY = {
    "municipality_code": "4305108",
    "municipality_name": "Caxias do Sul",
    "state": "RS",
    "batch_index": 0,
    "status": "running",
    "attempts": 1,
    "lease_token": "lease-token-1",
}

PAYLOAD = {
    "code": "4305108",
    "name": "Caxias do Sul",
    "state": "RS",
}


class FakeCoordinator:
    def __init__(self, report=None, error=None, on_search=None):
        self.report = report
        self.error = error
        self.on_search = on_search
        self.calls = []

    def search_municipality(self, municipality, limit_per_query=10):
        self.calls.append({
            "municipality": municipality,
            "limit_per_query": limit_per_query,
        })
        if self.on_search is not None:
            self.on_search()
        if self.error is not None:
            raise self.error
        return self.report


class FakeThread:
    def __init__(self):
        self.joined = False

    def join(self):
        self.joined = True


def make_report(matches=(), errors=()):
    return SimpleNamespace(matches=tuple(matches), errors=tuple(errors))


def make_match():
    return SimpleNamespace(
        result=SimpleNamespace(
            title="Auxiliar administrativo",
            url="https://empresa.example/vaga/123",
            snippet="Oportunidade de emprego",
            provider="test",
        ),
        category="municipality:RS",
        query="vagas Caxias do Sul RS",
        normalized_url="https://empresa.example/vaga/123",
    )


def patch_worker_dependencies(monkeypatch, claims=None):
    claims = list([MUNICIPALITY.copy()] if claims is None else claims)

    recovered = []
    monkeypatch.setattr(
        worker,
        "recover_interrupted_municipalities",
        lambda **kwargs: recovered.append(kwargs) or 0,
    )
    monkeypatch.setattr(
        worker,
        "claim_next_municipality",
        lambda batch_index: claims.pop(0) if claims else None,
    )
    monkeypatch.setattr(
        worker,
        "heartbeat_municipality",
        lambda code, token: {"municipality_code": code},
    )

    completed = []
    failed = []
    saved = []
    retried = []

    monkeypatch.setattr(
        worker,
        "mark_municipality_completed",
        lambda code, token: completed.append((code, token)),
    )
    monkeypatch.setattr(
        worker,
        "mark_municipality_failed",
        lambda code, error, token: failed.append((code, error, token)),
    )
    monkeypatch.setattr(
        worker,
        "save_discovery_result",
        lambda **kwargs: saved.append(kwargs),
    )

    def fake_retry(code, max_attempts):
        retried.append((code, max_attempts))
        if len(retried) >= max_attempts:
            raise ValueError("Limite de tentativas atingido.")
        return {"municipality_code": code, "status": "pending"}

    monkeypatch.setattr(worker, "retry_failed_municipality", fake_retry)

    return {
        "completed": completed,
        "failed": failed,
        "saved": saved,
        "retried": retried,
        "recovered": recovered,
    }


def test_worker_completes_municipality_successfully(monkeypatch):
    dependencies = patch_worker_dependencies(monkeypatch)
    coordinator = FakeCoordinator(report=make_report())

    stats = worker.run_worker(
        coordinator,
        batch_index=0,
        max_municipalities=1,
        heartbeat_interval=60,
    )

    assert stats["processed"] == 1
    assert stats["completed"] == 1
    assert stats["failed"] == 0
    assert stats["results_saved"] == 0
    assert dependencies["completed"] == [("4305108", "lease-token-1")]
    assert dependencies["failed"] == []
    assert coordinator.calls[0]["municipality"] == PAYLOAD
    assert coordinator.calls[0]["limit_per_query"] == 10


def test_worker_saves_discovery_matches(monkeypatch):
    dependencies = patch_worker_dependencies(monkeypatch)
    coordinator = FakeCoordinator(report=make_report(matches=[make_match()]))

    stats = worker.run_worker(coordinator, batch_index=0, max_municipalities=1)

    assert stats["completed"] == 1
    assert stats["results_saved"] == 1
    assert len(dependencies["saved"]) == 1
    saved = dependencies["saved"][0]
    assert saved["municipality_code"] == "4305108"
    assert saved["municipality_name"] == "Caxias do Sul"
    assert saved["state"] == "RS"
    assert saved["category"] == "municipality:RS"
    assert saved["normalized_url"] == "https://empresa.example/vaga/123"


def test_worker_retries_failed_municipality(monkeypatch):
    claims = []
    for attempt in range(1, 4):
        claim = MUNICIPALITY.copy()
        claim["attempts"] = attempt
        claim["lease_token"] = f"lease-token-{attempt}"
        claims.append(claim)

    dependencies = patch_worker_dependencies(monkeypatch, claims=claims)
    coordinator = FakeCoordinator(error=RuntimeError("Falha simulada na pesquisa."))
    stats = worker.run_worker(coordinator, batch_index=0, max_municipalities=3)

    assert stats["processed"] == 3
    assert stats["failed"] == 3
    assert stats["completed"] == 0
    assert stats["retried"] == 2
    assert len(dependencies["failed"]) == 3
    assert len(dependencies["retried"]) == 3
    assert all(
        max_attempts == worker.MAX_MUNICIPALITY_ATTEMPTS
        for _, max_attempts in dependencies["retried"]
    )


def test_worker_preserves_partial_results_when_report_has_errors(monkeypatch):
    dependencies = patch_worker_dependencies(monkeypatch)
    report = make_report(
        matches=[make_match()],
        errors=[SimpleNamespace(message="Uma consulta falhou.")],
    )
    stats = worker.run_worker(
        FakeCoordinator(report=report),
        batch_index=0,
        max_municipalities=1,
    )

    assert stats["failed"] == 1
    assert stats["completed"] == 0
    assert stats["results_saved"] == 1
    assert len(dependencies["saved"]) == 1
    assert len(dependencies["failed"]) == 1
    assert "Uma consulta falhou." in dependencies["failed"][0][1]


def test_worker_rejects_invalid_coordinator():
    with pytest.raises(ValueError):
        worker.run_worker(object(), batch_index=0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"batch_index": -1},
        {"max_municipalities": 0},
        {"max_municipalities": True},
        {"limit_per_query": 0},
        {"limit_per_query": 21},
        {"heartbeat_interval": 0},
        {"stale_after_seconds": -1},
        {"stale_after_seconds": 0},
        {"stale_after_seconds": True},
        {"stale_after_seconds": 1.5},
        {"stale_after_seconds": "3600"},
    ],
)
def test_worker_rejects_invalid_parameters(kwargs):
    coordinator = FakeCoordinator(report=make_report())
    parameters = {"batch_index": 0}
    parameters.update(kwargs)
    with pytest.raises(ValueError):
        worker.run_worker(coordinator, **parameters)


def test_worker_accepts_positive_stale_after_seconds(monkeypatch):
    dependencies = patch_worker_dependencies(monkeypatch, claims=[])

    stats = worker.run_worker(
        FakeCoordinator(report=make_report()),
        batch_index=0,
        max_municipalities=1,
        stale_after_seconds=1,
    )

    assert stats["processed"] == 0
    assert dependencies["recovered"] == [{
        "batch_index": 0,
        "stale_after_seconds": 1,
    }]


def test_worker_rejects_invalid_claimed_municipality(monkeypatch):
    invalid_claim = MUNICIPALITY.copy()
    invalid_claim["municipality_code"] = "123"
    dependencies = patch_worker_dependencies(monkeypatch, claims=[invalid_claim])
    coordinator = FakeCoordinator(report=make_report())

    stats = worker.run_worker(
        coordinator,
        batch_index=0,
        max_municipalities=1,
    )

    assert stats["failed"] == 1
    assert stats["completed"] == 0
    assert coordinator.calls == []
    assert dependencies["failed"] == []


def test_worker_stops_heartbeat_before_completion(monkeypatch):
    dependencies = patch_worker_dependencies(monkeypatch)
    stop_event = Event()
    fake_thread = FakeThread()
    heartbeat_errors = []
    monkeypatch.setattr(
        worker,
        "_start_heartbeat",
        lambda code, token, interval: (stop_event, fake_thread, heartbeat_errors),
    )

    stats = worker.run_worker(
        FakeCoordinator(report=make_report()),
        batch_index=0,
        max_municipalities=1,
    )

    assert stats["completed"] == 1
    assert stop_event.is_set()
    assert fake_thread.joined
    assert dependencies["completed"] == [("4305108", "lease-token-1")]


def test_worker_marks_failure_when_heartbeat_was_lost_before_save(monkeypatch):
    dependencies = patch_worker_dependencies(monkeypatch)
    stop_event = Event()
    fake_thread = FakeThread()
    heartbeat_errors = ["Reserva perdida durante a pesquisa."]
    monkeypatch.setattr(
        worker,
        "_start_heartbeat",
        lambda code, token, interval: (stop_event, fake_thread, heartbeat_errors),
    )

    coordinator = FakeCoordinator(report=make_report(matches=[make_match()]))
    stats = worker.run_worker(coordinator, batch_index=0, max_municipalities=1)

    assert stats["failed"] == 1
    assert stats["completed"] == 0
    assert fake_thread.joined
    assert stop_event.is_set()
    assert dependencies["saved"] == []
    assert "Reserva perdida" in stats["errors"][0]["error"]


def test_worker_detects_heartbeat_loss_during_search_before_saving(monkeypatch):
    dependencies = patch_worker_dependencies(monkeypatch)
    stop_event = Event()
    fake_thread = FakeThread()
    heartbeat_errors = []
    monkeypatch.setattr(
        worker,
        "_start_heartbeat",
        lambda code, token, interval: (stop_event, fake_thread, heartbeat_errors),
    )

    def lose_lease_during_search():
        heartbeat_errors.append("Lease expirado durante a pesquisa.")

    coordinator = FakeCoordinator(
        report=make_report(matches=[make_match()]),
        on_search=lose_lease_during_search,
    )
    stats = worker.run_worker(coordinator, batch_index=0, max_municipalities=1)

    assert len(coordinator.calls) == 1
    assert stats["failed"] == 1
    assert stats["completed"] == 0
    assert dependencies["saved"] == []
    assert dependencies["completed"] == []
    assert fake_thread.joined
    assert stop_event.is_set()
    assert "Lease expirado" in stats["errors"][0]["error"]


def test_heartbeat_loop_renews_until_stop_event_is_set(monkeypatch):
    calls = []
    first_heartbeat = Event()

    def fake_heartbeat(code, token):
        calls.append((code, token))
        first_heartbeat.set()

    monkeypatch.setattr(worker, "heartbeat_municipality", fake_heartbeat)
    stop_event = Event()
    heartbeat_errors = []
    thread = Thread(
        target=worker._heartbeat_loop,
        args=("4305108", "lease-token-1", stop_event, heartbeat_errors, 0.01),
    )

    thread.start()
    assert first_heartbeat.wait(timeout=1)
    stop_event.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert calls
    assert all(call == ("4305108", "lease-token-1") for call in calls)
    assert heartbeat_errors == []


def test_heartbeat_loop_records_error_and_stops_after_lease_loss(monkeypatch):
    calls = []

    def failing_heartbeat(code, token):
        calls.append((code, token))
        raise RuntimeError("Lease perdido.")

    monkeypatch.setattr(worker, "heartbeat_municipality", failing_heartbeat)
    stop_event = Event()
    heartbeat_errors = []
    thread = Thread(
        target=worker._heartbeat_loop,
        args=("4305108", "lease-token-1", stop_event, heartbeat_errors, 0.01),
    )

    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert calls == [("4305108", "lease-token-1")]
    assert heartbeat_errors == ["Lease perdido."]


def test_stop_heartbeat_sets_event_and_joins_thread():
    stop_event = Event()
    fake_thread = FakeThread()

    worker._stop_heartbeat(stop_event, fake_thread)

    assert stop_event.is_set()
    assert fake_thread.joined


def test_stop_heartbeat_accepts_missing_thread_or_event():
    worker._stop_heartbeat(None, FakeThread())
    worker._stop_heartbeat(Event(), None)


def test_worker_does_not_complete_when_final_heartbeat_fails(monkeypatch):
    dependencies = patch_worker_dependencies(monkeypatch)
    heartbeat_calls = []

    def heartbeat(code, token):
        heartbeat_calls.append((code, token))
        if len(heartbeat_calls) == 2:
            raise RuntimeError("Lease perdido na confirmação final.")
        return {"municipality_code": code}

    monkeypatch.setattr(worker, "heartbeat_municipality", heartbeat)
    stats = worker.run_worker(
        FakeCoordinator(report=make_report()),
        batch_index=0,
        max_municipalities=1,
    )

    assert stats["failed"] == 1
    assert stats["completed"] == 0
    assert dependencies["completed"] == []
    assert len(dependencies["failed"]) == 1
    assert len(heartbeat_calls) == 2


def test_worker_does_not_start_when_stale_recovery_validation_fails(monkeypatch):
    dependencies = patch_worker_dependencies(monkeypatch)
    coordinator = FakeCoordinator(report=make_report())

    with pytest.raises(ValueError, match="stale_after_seconds"):
        worker.run_worker(
            coordinator,
            batch_index=0,
            stale_after_seconds=0,
        )

    assert dependencies["recovered"] == []
    assert coordinator.calls == []


@pytest.mark.parametrize(
    "claimed, expected",
    [
        (
            {
                "municipality_code": "4305108",
                "municipality_name": "Caxias do Sul",
                "state": "RS",
            },
            {"code": "4305108", "name": "Caxias do Sul", "state": "RS"},
        ),
        (
            {
                "municipality_code": "3509502",
                "municipality_name": "Campinas",
                "state": "SP",
            },
            {"code": "3509502", "name": "Campinas", "state": "SP"},
        ),
    ],
)
def test_municipality_payload_accepts_code_matching_state(claimed, expected):
    assert worker._municipality_payload(claimed) == expected


def test_municipality_payload_rejects_code_from_another_state():
    claimed = {
        "municipality_code": "3509502",
        "municipality_name": "Campinas",
        "state": "RS",
    }

    with pytest.raises(ValueError, match="não corresponde à UF"):
        worker._municipality_payload(claimed)


def test_municipality_payload_rejects_unknown_state():
    claimed = {
        "municipality_code": "4305108",
        "municipality_name": "Caxias do Sul",
        "state": "XX",
    }

    with pytest.raises(ValueError, match="UF brasileira desconhecida"):
        worker._municipality_payload(claimed)


def test_municipality_payload_normalizes_lowercase_state():
    claimed = {
        "municipality_code": "4305108",
        "municipality_name": "Caxias do Sul",
        "state": "rs",
    }

    assert worker._municipality_payload(claimed) == {
        "code": "4305108",
        "name": "Caxias do Sul",
        "state": "RS",
        }
    
