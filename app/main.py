
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


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("busca-vagas-robo-2")


def require_source_admin(token: str | None):
    """Protege o gerenciamento de fontes e pesquisas."""

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
    except Exception:
        logger.exception("Falha ao inicializar o banco de dados")
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
    return {
        "status": "ok",
        "service": "busca-vagas-robo-2",
        "publishing_enabled": False,
    }


@app.post("/api/candidates")
def create_candidate(
    candidate: CandidateIn,
    x_admin_token: str | None = Header(
        default=None,
        alias="X-Admin-Token",
    ),
):
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
        return list_candidates(status=status, limit=limit)

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

    try:
        engine = SearchEngine(
            providers=[TavilySearchProvider()]
        )

        results = engine.search(
            query=query,
            limit=limit,
        )

        return {
            "query": " ".join(query.split()),
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
    
