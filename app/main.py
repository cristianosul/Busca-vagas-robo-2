
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Header

from app.models import (
    CandidateIn,
    SourceCreate,
    SourceStatusUpdate,
)
from app.repository import (
    init_db,
    connect,
    execute,
    save_candidate,
    list_candidates,
    save_source,
    get_source_record,
    list_sources,
    update_source_status,
)
from app.search_engine import (
    SearchEngine,
    TavilySearchProvider,
)
from app.municipality_scheduler import (
    init_municipality_scheduler,
    get_current_batch,
    get_batch_progress,
    list_batch_municipalities,
    register_municipality_batch,
)
from app.national_discovery import (
    MunicipalityCatalogError,
    MAX_MUNICIPALITIES_PER_BATCH,
    load_brazilian_municipalities,
    municipality_batch_count,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("busca-vagas-robo-2")


def require_source_admin(token: str | None):
    """Protege o gerenciamento administrativo."""

    expected_token = os.getenv("SOURCE_ADMIN_TOKEN", "").strip()

    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="Gerenciamento administrativo não configurado.",
        )

    if not token or not secrets.compare_digest(token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Token administrativo inválido.",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Inicializando Busca Vagas — Robô 2")

    try:
        init_db()
        logger.info("Banco de dados inicializado com sucesso")

        init_municipality_scheduler()
        logger.info("Agendador municipal inicializado com sucesso")

    except Exception:
        logger.exception("Falha ao inicializar o banco de dados ou agendador")
        raise

    yield

    logger.info("Encerrando Busca Vagas — Robô 2")


app = FastAPI(
    title="Busca Vagas — Robô 2",
    description=(
        "Pesquisador nacional independente. "
        "Recepção e triagem em ambiente separado."
    ),
    version="0.3.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    """Verifica se o processo da API está ativo."""

    return {
        "status": "ok",
        "service": "busca-vagas-robo-2",
        "publishing_enabled": False,
    }


@app.get("/ready")
def readiness():
    """Verifica se a API consegue acessar o banco de dados."""

    conn = None

    try:
        conn = connect()
        execute(conn, "SELECT 1").fetchone()

        return {
            "status": "ready",
            "database": "ok",
            "publishing_enabled": False,
        }

    except Exception:
        logger.exception("Verificação de disponibilidade do banco falhou")

        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "database": "unavailable",
            },
        )

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.exception(
                    "Erro ao fechar conexão da verificação de disponibilidade"
                )


@app.post("/api/candidates")
def create_candidate(
    candidate: CandidateIn,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    """Registra uma oportunidade sem permitir aprovação automática indevida."""

    require_source_admin(x_admin_token)

    try:
        return save_candidate(candidate)

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    except Exception:
        logger.exception("Erro ao registrar oportunidade")

        raise HTTPException(
            status_code=500,
            detail="Não foi possível registrar a oportunidade.",
        )


@app.get("/api/candidates")
def get_candidates(
    status: str | None = Query(
        default=None,
        pattern="^(approved|review|rejected)$",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    require_source_admin(x_admin_token)

    try:
        return list_candidates(
            status=status,
            limit=limit,
        )

    except Exception:
        logger.exception("Erro ao consultar oportunidades")

        raise HTTPException(
            status_code=500,
            detail="Não foi possível consultar as oportunidades.",
        )


@app.post("/api/discovery/search")
def discover_web_results(
    query: str = Query(
        ...,
        min_length=3,
        max_length=300,
    ),
    limit: int = Query(
        default=10,
        ge=1,
        le=20,
    ),
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    """Pesquisa na web sem cadastrar ou publicar oportunidades."""

    require_source_admin(x_admin_token)

    normalized_query = " ".join(query.split())

    if len(normalized_query) < 3:
        raise HTTPException(
            status_code=422,
            detail="A consulta deve conter pelo menos 3 caracteres úteis.",
        )

    try:
        engine = SearchEngine(
            providers=[TavilySearchProvider()]
        )

        results = engine.search(
            query=normalized_query,
            limit=limit,
        )

        return {
            "query": normalized_query,
            "provider": "tavily",
            "total": len(results),
            "results": [
                {
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                    "provider": result.provider,
                }
                for result in results
            ],
            "saved": False,
            "publishing_enabled": False,
        }

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    except RuntimeError as exc:
        message = str(exc)

        if "TAVILY_API_KEY" in message:
            status_code = 503
        elif "Limite de utilização" in message:
            status_code = 429
        elif "temporariamente indisponível" in message:
            status_code = 503
        else:
            status_code = 502

        raise HTTPException(
            status_code=status_code,
            detail=message,
        ) from exc

    except Exception:
        logger.exception("Erro inesperado na pesquisa web")

        raise HTTPException(
            status_code=500,
            detail="Não foi possível realizar a pesquisa.",
        )


@app.get("/admin/municipalities/batches/current")
def current_municipality_batch(
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    """Consulta o lote municipal atualmente registrado."""

    require_source_admin(x_admin_token)

    try:
        return {
            "current_batch": get_current_batch(),
        }

    except Exception:
        logger.exception("Erro ao consultar lote municipal atual")

        raise HTTPException(
            status_code=500,
            detail="Não foi possível consultar o lote atual.",
        )


@app.get("/admin/municipalities/batches/{batch_index}/progress")
def municipality_batch_progress(
    batch_index: int,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    """Consulta o progresso de um lote municipal."""

    require_source_admin(x_admin_token)

    try:
        return get_batch_progress(batch_index)

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    except Exception:
        logger.exception("Erro ao consultar progresso municipal")

        raise HTTPException(
            status_code=500,
            detail="Não foi possível consultar o progresso do lote.",
        )


@app.get("/admin/municipalities/batches/{batch_index}")
def get_municipality_batch(
    batch_index: int,
    status: str | None = Query(
        default=None,
        pattern="^(pending|running|completed|failed)$",
    ),
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    """Lista os municípios registrados em um lote."""

    require_source_admin(x_admin_token)

    try:
        municipalities = list_batch_municipalities(
            batch_index=batch_index,
            status=status,
        )

        return {
            "batch_index": batch_index,
            "status_filter": status,
            "total": len(municipalities),
            "municipalities": municipalities,
        }

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    except Exception:
        logger.exception("Erro ao listar municípios do lote")

        raise HTTPException(
            status_code=500,
            detail="Não foi possível listar os municípios.",
        )


@app.post("/admin/municipalities/batches/{batch_index}/register")
def register_municipality_batch_endpoint(
    batch_index: int,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    """Registra no banco os municípios do lote solicitado."""

    require_source_admin(x_admin_token)

    if (
        isinstance(batch_index, bool)
        or batch_index < 0
    ):
        raise HTTPException(
            status_code=422,
            detail="O índice do lote deve ser não negativo.",
        )

    try:
        total_batches = municipality_batch_count()

        if batch_index >= total_batches:
            raise HTTPException(
                status_code=404,
                detail="Lote municipal fora do intervalo disponível.",
            )

        municipalities = load_brazilian_municipalities()

        start = batch_index * MAX_MUNICIPALITIES_PER_BATCH
        end = start + MAX_MUNICIPALITIES_PER_BATCH

        selected_batch = municipalities[start:end]

        if not selected_batch:
            raise HTTPException(
                status_code=404,
                detail="Nenhum município encontrado para esse lote.",
            )

        progress = register_municipality_batch(
            batch_index=batch_index,
            municipalities=selected_batch,
        )

        return {
            "message": "Lote municipal registrado com sucesso.",
            "batch_index": batch_index,
            "municipalities_in_catalog": len(selected_batch),
            "progress": progress,
            "research_started": False,
            "publishing_enabled": False,
        }

    except HTTPException:
        raise

    except MunicipalityCatalogError as exc:
        logger.exception("Erro ao consultar catálogo municipal do IBGE")

        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    except Exception:
        logger.exception("Erro ao registrar lote municipal")

        raise HTTPException(
            status_code=500,
            detail="Não foi possível registrar o lote municipal.",
        )


@app.post("/api/sources", status_code=201)
def create_source(
    source: SourceCreate,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    require_source_admin(x_admin_token)

    if source.enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "Uma nova fonte deve ser cadastrada desativada. "
                "A ativação depende de um processo interno de verificação."
            ),
        )

    try:
        return save_source(source)

    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    except Exception:
        logger.exception("Erro ao cadastrar fonte")

        raise HTTPException(
            status_code=500,
            detail="Não foi possível cadastrar a fonte.",
        )


@app.get("/api/sources")
def get_sources(
    enabled_only: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    require_source_admin(x_admin_token)

    try:
        return list_sources(
            enabled_only=enabled_only,
            limit=limit,
        )

    except Exception:
        logger.exception("Erro ao consultar fontes")

        raise HTTPException(
            status_code=500,
            detail="Não foi possível consultar as fontes.",
        )


@app.get("/api/sources/{source_id}")
def get_source(
    source_id: str,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    require_source_admin(x_admin_token)

    try:
        source = get_source_record(source_id)

        if not source:
            raise HTTPException(
                status_code=404,
                detail="Fonte não encontrada.",
            )

        return source

    except HTTPException:
        raise

    except Exception:
        logger.exception("Erro ao consultar fonte")

        raise HTTPException(
            status_code=500,
            detail="Não foi possível consultar a fonte.",
        )


@app.patch("/api/sources/{source_id}")
def change_source_status(
    source_id: str,
    update: SourceStatusUpdate,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
    require_source_admin(x_admin_token)

    try:
        if update.enabled:
            source = get_source_record(source_id)

            if not source:
                raise HTTPException(
                    status_code=404,
                    detail="Fonte não encontrada.",
                )

            raise HTTPException(
                status_code=403,
                detail=(
                    "A ativação direta de fontes está bloqueada. "
                    "É necessária uma verificação interna antes da ativação."
                ),
            )

        source = update_source_status(
            source_id,
            update.enabled,
        )

        if not source:
            raise HTTPException(
                status_code=404,
                detail="Fonte não encontrada.",
            )

        return source

    except HTTPException:
        raise

    except Exception:
        logger.exception("Erro ao atualizar fonte")

        raise HTTPException(
            status_code=500,
            detail="Não foi possível atualizar a fonte.",
        )


@app.get("/")
def root():
    return {
        "name": "Busca Vagas — Robô 2",
        "stage": "estrutura de triagem",
        "message": (
            "Pesquisa nacional e publicação externa "
            "ainda desativadas."
        ),
        "publishing_enabled": False,
    }
    
