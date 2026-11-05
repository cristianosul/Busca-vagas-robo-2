Python

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from app.models import CandidateIn
from app.repository import init_db, save_candidate, list_candidates

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("busca-vagas-robo-2")


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
    version="0.2.1",
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
def create_candidate(candidate: CandidateIn):
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
):
    try:
        return list_candidates(status=status, limit=limit)

    except Exception:
        logger.exception("Erro ao consultar oportunidades")
        raise HTTPException(
            status_code=500,
            detail="Não foi possível consultar as oportunidades.",
        )


@app.get("/")
def root():
    return {
        "name": "Busca Vagas — Robô 2",
        "stage": "estrutura de triagem",
        "message": "Pesquisa nacional e publicação externa ainda desativadas.",
        "publishing_enabled": False,
    }
