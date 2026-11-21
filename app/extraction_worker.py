"""Worker manual e limitado para extrair páginas de vagas já descobertas.

Este módulo conecta a fila persistente do repository ao leitor HTTPS seguro e
à extração HTML sem rede. Não inicia junto da API e não pesquisa novas URLs.

Execução manual:
    python -m app.extraction_worker

Execução limitada:
    python -m app.extraction_worker --batch-size 5 --enqueue-limit 50

A execução só ocorre quando este módulo é chamado explicitamente.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from typing import Any

from app.job_extraction import extract_jobs_from_html
from app.job_page_fetcher import fetch_job_page
from app.repository import (
    claim_next_extraction,
    enqueue_pending_extractions,
    init_db,
    mark_extraction_completed,
    mark_extraction_failed,
    recover_interrupted_extractions,
    save_extracted_job,
)

logger = logging.getLogger("extraction_worker")

DEFAULT_BATCH_SIZE = 1
DEFAULT_ENQUEUE_LIMIT = 100
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LEASE_SECONDS = 300
DEFAULT_RETRY_DELAY_SECONDS = 300
DEFAULT_STALE_AFTER_SECONDS = 900


@dataclass
class WorkerSummary:
    """Resumo de uma execução limitada do worker."""

    recovered: int = 0
    enqueued: int = 0
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    extracted: int = 0
    extraction_ids: list[int] = field(default_factory=list)


def _positive_integer(value: int, name: str, *, maximum: int = 1000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} deve ser um inteiro entre 1 e {maximum}.")
    return value


def _discovery_payload(claimed: dict[str, Any]) -> tuple[dict[str, Any], str, str | None]:
    """Valida o envelope da reserva e obtém URL e provedor sem confiar neles."""
    if not isinstance(claimed, dict):
        raise ValueError("A fila retornou uma reserva inválida.")

    discovery = claimed.get("discovery_result")
    if not isinstance(discovery, dict):
        raise ValueError("A reserva não contém o resultado da descoberta.")

    result = discovery.get("result")
    if not isinstance(result, dict):
        raise ValueError("O resultado bruto da descoberta não é um objeto.")

    url = result.get("url") or discovery.get("normalized_url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("A descoberta não contém uma URL para leitura.")

    provider = result.get("provider")
    source_name = provider.strip() if isinstance(provider, str) and provider.strip() else None
    return discovery, url.strip(), source_name


def process_claimed_extraction(
    claimed: dict[str, Any],
    *,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
) -> dict[str, Any]:
    """Lê, extrai e persiste uma reserva; nunca cria nem pontua candidatos."""
    retry_delay_seconds = _positive_integer(
        retry_delay_seconds,
        "retry_delay_seconds",
        maximum=7 * 24 * 3600,
    )
    queue_id = claimed.get("id") if isinstance(claimed, dict) else None
    lease_token = claimed.get("lease_token") if isinstance(claimed, dict) else None
    if isinstance(queue_id, bool) or not isinstance(queue_id, int) or queue_id < 1:
        raise ValueError("A reserva não contém um ID de fila válido.")
    if not isinstance(lease_token, str) or not lease_token:
        raise ValueError("A reserva não contém um token de lease válido.")

    try:
        discovery, source_url, source_name = _discovery_payload(claimed)
        fetched = fetch_job_page(source_url)
        extracted_jobs = extract_jobs_from_html(
            fetched.html,
            page_url=fetched.final_url,
        )
        if not extracted_jobs:
            raise RuntimeError("Nenhum registro de vaga foi extraído da página.")

        saved_ids: list[int] = []
        for extracted_job in extracted_jobs:
            saved = save_extracted_job(
                extracted_job,
                discovery_result_id=discovery["id"],
                source_name=source_name,
                source_url=source_url,
            )
            if not isinstance(saved, dict) or not isinstance(saved.get("id"), int):
                raise RuntimeError("O repositório não confirmou a persistência da extração.")
            saved_ids.append(saved["id"])

        completed = mark_extraction_completed(queue_id, lease_token)
        if completed is None:
            # A extração pode ter sido salva, mas um worker com lease vencido
            # nunca pode concluir uma tarefa que já foi recuperada por outro.
            raise RuntimeError("Lease perdido antes da conclusão da tarefa.")

        return {
            "status": "completed",
            "queue_id": queue_id,
            "extracted_count": len(saved_ids),
            "extraction_ids": saved_ids,
        }

    except Exception as exc:
        error_message = str(exc).strip() or exc.__class__.__name__
        try:
            failed = mark_extraction_failed(
                queue_id,
                lease_token,
                error_message[:2000],
                retry_delay_seconds=retry_delay_seconds,
            )
        except Exception:
            logger.exception("Não foi possível registrar a falha da tarefa %s.", queue_id)
            raise

        if failed is None:
            logger.warning(
                "Tarefa %s falhou após perder o lease; o worker atual não alterou a fila.",
                queue_id,
            )
        else:
            logger.warning(
                "Extração da tarefa %s falhou; status=%s; motivo=%s",
                queue_id,
                failed.get("status"),
                error_message,
            )
        return {
            "status": "failed",
            "queue_id": queue_id,
            "queue_result": failed,
            "error": error_message[:2000],
        }


def run_worker(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    enqueue_limit: int = DEFAULT_ENQUEUE_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> WorkerSummary:
    """Executa um lote finito; não mantém processo em segundo plano."""
    batch_size = _positive_integer(batch_size, "batch_size", maximum=100)
    enqueue_limit = _positive_integer(enqueue_limit, "enqueue_limit", maximum=5000)
    max_attempts = _positive_integer(max_attempts, "max_attempts", maximum=100)
    lease_seconds = _positive_integer(lease_seconds, "lease_seconds", maximum=86_400)
    retry_delay_seconds = _positive_integer(
        retry_delay_seconds,
        "retry_delay_seconds",
        maximum=7 * 24 * 3600,
    )
    if (
        isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, int)
        or not 0 <= stale_after_seconds <= 7 * 24 * 3600
    ):
        raise ValueError("stale_after_seconds deve ser um inteiro entre 0 e sete dias.")

    init_db()
    summary = WorkerSummary()
    summary.recovered = recover_interrupted_extractions(
        stale_after_seconds=stale_after_seconds,
    )
    summary.enqueued = enqueue_pending_extractions(
        limit=enqueue_limit,
        max_attempts=max_attempts,
    )

    for _ in range(batch_size):
        claimed = claim_next_extraction(lease_seconds=lease_seconds)
        if claimed is None:
            break

        summary.claimed += 1
        outcome = process_claimed_extraction(
            claimed,
            retry_delay_seconds=retry_delay_seconds,
        )
        if outcome["status"] == "completed":
            summary.completed += 1
            summary.extracted += outcome["extracted_count"]
            summary.extraction_ids.extend(outcome["extraction_ids"])
        else:
            summary.failed += 1

    logger.info(
        "Lote de extração concluído: recuperadas=%d enfileiradas=%d reservadas=%d "
        "concluídas=%d falhas=%d extrações=%d",
        summary.recovered,
        summary.enqueued,
        summary.claimed,
        summary.completed,
        summary.failed,
        summary.extracted,
    )
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Processa um lote limitado de páginas de vagas já descobertas."
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--enqueue-limit", type=int, default=DEFAULT_ENQUEUE_LIMIT)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument(
        "--retry-delay-seconds",
        type=int,
        default=DEFAULT_RETRY_DELAY_SECONDS,
    )
    parser.add_argument(
        "--stale-after-seconds",
        type=int,
        default=DEFAULT_STALE_AFTER_SECONDS,
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    level = getattr(logging, str(args.log_level).upper(), None)
    if not isinstance(level, int):
        parser.error("--log-level inválido.")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        summary = run_worker(
            batch_size=args.batch_size,
            enqueue_limit=args.enqueue_limit,
            max_attempts=args.max_attempts,
            lease_seconds=args.lease_seconds,
            retry_delay_seconds=args.retry_delay_seconds,
            stale_after_seconds=args.stale_after_seconds,
        )
    except (ValueError, RuntimeError) as exc:
        logger.error("Worker não executado: %s", exc)
        return 2

    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "WorkerSummary",
    "main",
    "process_claimed_extraction",
    "run_worker",
]
