"""
Worker independente de pesquisa municipal do Busca Vagas — Robô 2.

Execução manual:
    python -m app.municipality_worker --batch-index 0

Execução limitada:
    python -m app.municipality_worker --batch-index 0 --max-municipalities 5

O worker não é iniciado automaticamente pela API.
"""

import argparse
import logging
import os
import threading
from typing import Any

from app.municipality_scheduler import (
    MAX_MUNICIPALITY_ATTEMPTS,
    claim_next_municipality,
    heartbeat_municipality,
    init_municipality_scheduler,
    mark_municipality_completed,
    mark_municipality_failed,
    recover_interrupted_municipalities,
    retry_failed_municipality,
)
from app.national_discovery import (
    UF_IBGE_CODES,
    NationalDiscoveryCoordinator,
)
from app.repository import (
    init_db,
    save_discovery_result,
)
from app.search_engine import (
    SearchEngine,
    TavilySearchProvider,
)

logger = logging.getLogger("municipality_worker")

DEFAULT_MAX_MUNICIPALITIES = 1
DEFAULT_HEARTBEAT_INTERVAL = 60
DEFAULT_STALE_AFTER_SECONDS = 3600
DEFAULT_LIMIT_PER_QUERY = 10


def _validate_positive_integer(value: int, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise ValueError(
            f"{field} deve ser um inteiro positivo."
        )

    return value


def _validate_batch_index(batch_index: int) -> int:
    if (
        isinstance(batch_index, bool)
        or not isinstance(batch_index, int)
        or batch_index < 0
    ):
        raise ValueError(
            "batch_index deve ser um inteiro não negativo."
        )

    return batch_index


def _heartbeat_loop(
    municipality_code: str,
    lease_token: str,
    stop_event: threading.Event,
    heartbeat_errors: list[str],
    interval: int,
):
    """
    Renova a reserva periodicamente.
    Se perder a propriedade da reserva, registra o erro
    e encerra o heartbeat.
    """

    while not stop_event.wait(interval):
        try:
            heartbeat_municipality(
                municipality_code,
                lease_token,
            )
        except Exception as exc:
            heartbeat_errors.append(str(exc))

            logger.exception(
                "Falha no heartbeat do município %s.",
                municipality_code,
            )
            return


def _start_heartbeat(
    municipality_code: str,
    lease_token: str,
    interval: int,
):
    stop_event = threading.Event()
    heartbeat_errors = []

    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(
            municipality_code,
            lease_token,
            stop_event,
            heartbeat_errors,
            interval,
        ),
        name=f"municipality-heartbeat-{municipality_code}",
        daemon=True,
    )

    thread.start()
    return stop_event, thread, heartbeat_errors


def _stop_heartbeat(
    stop_event: threading.Event | None,
    thread: threading.Thread | None,
):
    """
    Para o heartbeat e aguarda o encerramento completo da thread.

    Não deixa uma renovação de reserva continuar depois
    da conclusão ou falha do município.
    """

    if stop_event is None or thread is None:
        return

    stop_event.set()
    thread.join()


def _municipality_payload(claimed: dict) -> dict:
    """
    Adapta os campos do scheduler ao contrato
    de search_municipality().
    """

    if not isinstance(claimed, dict):
        raise ValueError(
            "O scheduler retornou uma reserva inválida."
        )

    code = claimed.get("municipality_code")
    name = claimed.get("municipality_name")
    state = claimed.get("state")
    if (
        not isinstance(code, str)
        or len(code) != 7
        or not code.isdigit()
    ):
        raise ValueError(
            "O scheduler retornou um código IBGE inválido."
        )

    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            "O scheduler retornou um nome municipal inválido."
        )
    if (
        not isinstance(state, str)
        or len(state) != 2
        or not state.isascii()
        or not state.isalpha()
    ):
        raise ValueError(
            "O scheduler retornou uma UF inválida."
        )

    state = state.upper()
    expected_uf_code = UF_IBGE_CODES.get(state)

    if expected_uf_code is None:
        raise ValueError(
            "O scheduler retornou uma UF brasileira desconhecida."
        )

    if code[:2] != expected_uf_code:
        raise ValueError(
            "O código IBGE do município não corresponde à UF informada."
        )

    return {
        "code": code,
        "name": name.strip(),
        "state": state,
    }


def _save_report(
    municipality: dict,
    report: Any,
) -> int:
    """
    Persiste os resultados brutos encontrados.
    Preserva a categoria e a consulta que originaram
    cada resultado.
    """

    matches = getattr(report, "matches", None)

    if matches is None:
        raise RuntimeError(
            "O relatório de pesquisa não contém matches."
        )

    saved = 0
    for match in matches:
        result = getattr(match, "result", None)
        category = getattr(match, "category", None)
        query = getattr(match, "query", None)
        normalized_url = getattr(
            match,
            "normalized_url",
            None,
        )

        if result is None:
            raise ValueError(
                "A pesquisa retornou um resultado sem conteúdo."
            )
        if (
            not isinstance(category, str)
            or not category.strip()
        ):
            raise ValueError(
                "Resultado sem categoria de pesquisa."
            )

        if (
            not isinstance(query, str)
            or not query.strip()
        ):
            raise ValueError(
                "Resultado sem consulta de origem."
            )
        if (
            not isinstance(normalized_url, str)
            or not normalized_url.strip()
        ):
            raise ValueError(
                "Resultado sem URL normalizada."
            )
        save_discovery_result(
            municipality_code=municipality["code"],
            municipality_name=municipality["name"],
            state=municipality["state"],
            category=category,
            query=query,
            normalized_url=normalized_url,
            result=result,
        )

        saved += 1

    return saved


def _report_errors(report: Any) -> list[str]:
    """Converte os erros do relatório em mensagens legíveis."""

    errors = getattr(report, "errors", ())
    if not errors:
        return []

    return [
        str(getattr(error, "message", error))
        for error in errors
    ]


def _record_failure(
    municipality_code: str,
    message: str,
    lease_token: str,
) -> bool:
    """
    Registra a falha e solicita nova tentativa quando permitida.

    Retorna True quando o município volta para pending.
    """

    mark_municipality_failed(
        municipality_code,
        message,
        lease_token,
    )
    try:
        retry_failed_municipality(
            municipality_code,
            max_attempts=MAX_MUNICIPALITY_ATTEMPTS,
        )

        logger.info(
            "Município %s devolvido para nova tentativa.",
            municipality_code,
        )

        return True
    except ValueError:
        logger.warning(
            "Município %s não será reenfileirado: "
            "limite de %s tentativas atingido ou estado incompatível.",
            municipality_code,
            MAX_MUNICIPALITY_ATTEMPTS,
        )

        return False


def run_worker(
    coordinator: NationalDiscoveryCoordinator,
    *,
    batch_index: int,
    max_municipalities: int = DEFAULT_MAX_MUNICIPALITIES,
    limit_per_query: int = DEFAULT_LIMIT_PER_QUERY,
    heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> dict:
    """
    Executa uma quantidade limitada de tentativas de pesquisa.
    Aceita coordenadores compatíveis por comportamento,
    permitindo testes isolados com implementações simuladas.

    max_municipalities limita o número de tentativas,
    não o número de municípios únicos.
    """

    if not callable(
        getattr(coordinator, "search_municipality", None)
    ):
        raise ValueError(
            "O coordenador deve fornecer search_municipality()."
        )

    batch_index = _validate_batch_index(batch_index)
    max_municipalities = _validate_positive_integer(
        max_municipalities,
        "max_municipalities",
    )

    limit_per_query = _validate_positive_integer(
        limit_per_query,
        "limit_per_query",
    )

    heartbeat_interval = _validate_positive_integer(
        heartbeat_interval,
        "heartbeat_interval",
    )

    if limit_per_query > 20:
        raise ValueError(
            "limit_per_query não pode ultrapassar 20."
        )
    if (
        isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, int)
        or stale_after_seconds < 1
    ):
        raise ValueError(
            "stale_after_seconds deve ser um inteiro positivo."
        )

    stats = {
        "batch_index": batch_index,
        "recovered": 0,
        "processed": 0,
        "completed": 0,
        "failed": 0,
        "retried": 0,
        "results_saved": 0,
        "errors": [],
    }
    logger.info(
        "Iniciando worker no lote %s; limite de tentativas: %s.",
        batch_index,
        max_municipalities,
    )

    stats["recovered"] = recover_interrupted_municipalities(
        batch_index=batch_index,
        stale_after_seconds=stale_after_seconds,
    )

    while stats["processed"] < max_municipalities:
        claimed = claim_next_municipality(batch_index)
        if claimed is None:
            logger.info(
                "Não existem municípios pendentes no lote %s.",
                batch_index,
            )
            break

        municipality = None
        municipality_code = None
        lease_token = None

        stop_event = None
        heartbeat_thread = None
        heartbeat_errors = []

        try:
            municipality = _municipality_payload(claimed)
            municipality_code = municipality["code"]
            lease_token = claimed.get("lease_token")

            if (
                not isinstance(lease_token, str)
                or not lease_token.strip()
            ):
                raise RuntimeError(
                    "O scheduler não forneceu um lease_token válido."
                )

            lease_token = lease_token.strip()
            # Confirma a reserva antes de iniciar a pesquisa.
            heartbeat_municipality(
                municipality_code,
                lease_token,
            )

            (
                stop_event,
                heartbeat_thread,
                heartbeat_errors,
            ) = _start_heartbeat(
                municipality_code,
                lease_token,
                heartbeat_interval,
            )
            logger.info(
                "Pesquisando %s/%s (%s).",
                municipality["name"],
                municipality["state"],
                municipality_code,
            )

            report = coordinator.search_municipality(
                municipality,
                limit_per_query=limit_per_query,
            )
            if heartbeat_errors:
                raise RuntimeError(
                    "A reserva do município foi perdida: "
                    + heartbeat_errors[-1]
                )

            # Persiste resultados parciais antes de avaliar erros
            # das consultas individuais.
            saved = _save_report(
                municipality,
                report,
            )

            stats["results_saved"] += saved

            report_errors = _report_errors(report)
            if report_errors:
                raise RuntimeError(
                    "A pesquisa municipal apresentou falhas: "
                    + "; ".join(report_errors)
                )

            # Para e aguarda o encerramento completo da thread.
            _stop_heartbeat(
                stop_event,
                heartbeat_thread,
            )

            stop_event = None
            heartbeat_thread = None
            if heartbeat_errors:
                raise RuntimeError(
                    "A reserva do município foi perdida: "
                    + heartbeat_errors[-1]
                )

            # Confirma novamente a propriedade da reserva.
            heartbeat_municipality(
                municipality_code,
                lease_token,
            )

            mark_municipality_completed(
                municipality_code,
                lease_token,
            )
            stats["completed"] += 1

            logger.info(
                "Município concluído: %s/%s. "
                "Resultados salvos: %s.",
                municipality["name"],
                municipality["state"],
                saved,
            )

        except Exception as exc:
            message = str(exc)[:2000]

            logger.exception(
                "Falha na tentativa de pesquisa municipal."
            )

            stats["failed"] += 1
            stats["errors"].append({
                "municipality_code": municipality_code,
                "municipality_name": (
                    municipality["name"]
                    if municipality is not None
                    else None
                ),
                "error": message,
            })

            # Interrompe o heartbeat antes de alterar a reserva.
            _stop_heartbeat(
                stop_event,
                heartbeat_thread,
            )
            stop_event = None
            heartbeat_thread = None
            # Uma reserva malformada não pode ser alterada com
            # segurança. A recuperação de reservas stale poderá
            # tratar esse caso posteriormente.
            if (
                municipality_code is not None
                and isinstance(lease_token, str)
                and lease_token
            ):
                try:
                    retried = _record_failure(
                        municipality_code,
                        message,
                        lease_token,
                    )
                    if retried:
                        stats["retried"] += 1

                except Exception:
                    logger.exception(
                        "Não foi possível registrar ou reenfileirar "
                        "a falha do município %s. "
                        "A recuperação de reservas interrompidas "
                        "poderá tratar o caso posteriormente.",
                        municipality_code,
                    )
        finally:
            _stop_heartbeat(
                stop_event,
                heartbeat_thread,
            )

            stats["processed"] += 1

    logger.info(
        "Worker encerrado. Tentativas: %s; concluídos: %s; "
        "falhas: %s; reenfileirados: %s; resultados salvos: %s.",
        stats["processed"],
        stats["completed"],
        stats["failed"],
        stats["retried"],
        stats["results_saved"],
    )

    return stats


def build_default_coordinator() -> NationalDiscoveryCoordinator:
    """
    Constrói o mecanismo de pesquisa usando o provedor Tavily.
    """

    search_engine = SearchEngine([
        TavilySearchProvider(),
    ])

    return NationalDiscoveryCoordinator(search_engine)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Worker independente de pesquisa municipal "
            "do Busca Vagas — Robô 2."
        )
    )
    parser.add_argument(
        "--batch-index",
        type=int,
        required=True,
        help="Índice do lote municipal que será pesquisado.",
    )

    parser.add_argument(
        "--max-municipalities",
        type=int,
        default=DEFAULT_MAX_MUNICIPALITIES,
        help="Máximo de tentativas nesta execução. Padrão: 1.",
    )
    parser.add_argument(
        "--limit-per-query",
        type=int,
        default=DEFAULT_LIMIT_PER_QUERY,
        help="Resultados máximos por consulta. Padrão: 10.",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not os.getenv("TAVILY_API_KEY", "").strip():
        parser.error(
            "Configure TAVILY_API_KEY antes de iniciar o worker."
        )
    init_db()
    init_municipality_scheduler()

    coordinator = build_default_coordinator()

    stats = run_worker(
        coordinator,
        batch_index=args.batch_index,
        max_municipalities=args.max_municipalities,
        limit_per_query=args.limit_per_query,
    )

    if stats["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
