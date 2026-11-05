from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from app.models import CandidateIn
from app.repository import init_db, save_candidate, list_candidates


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Busca Vagas — Robô 2",
    description="Pesquisador nacional independente. Recepção e triagem em ambiente separado.",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "busca-vagas-robo-2", "publishing_enabled": False}


@app.post("/api/candidates")
def create_candidate(candidate: CandidateIn):
    try:
        # Nenhuma evidência é aceita do cliente; o pipeline interno deverá verificá-la.
        return save_candidate(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Não foi possível registrar a oportunidade.") from exc


@app.get("/api/candidates")
def get_candidates(
    status: str | None = Query(default=None, pattern="^(approved|review|rejected)$"),
    limit: int = Query(default=50, ge=1, le=200),
):
    return list_candidates(status=status, limit=limit)


@app.get("/")
def root():
    return {
        "name": "Busca Vagas — Robô 2",
        "stage": "estrutura de triagem",
        "message": "Pesquisa nacional e publicação externa ainda desativadas.",
        "publishing_enabled": False,
    }
