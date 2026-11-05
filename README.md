# Busca Vagas — Robô 2 (estrutura inicial)

Projeto independente do Robô 1. Esta versão **não pesquisa na internet e não publica vagas no Busca Vagas**. Ela prepara a recepção, classificação e armazenamento isolado de oportunidades para testes.

## Requisitos
- Python 3.10+
- pip

## Executar localmente
```bash
python -m venv .venv
# Windows:
.venv\\Scripts\\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload
```

Abra:
- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/docs`

## Segurança e limites desta etapa
- Banco SQLite local separado (`data/staging.db`).
- Não conectar ao banco de produção do Busca Vagas.
- Não publicar nem enviar vagas ao site.
- A aprovação depende de evidências verificadas; não marque os campos `*_verified` como verdadeiros sem realizar a checagem.
- A pesquisa por buscadores e a validação automática de links serão implementadas em etapas posteriores.
- Antes de hospedar em serviço com disco efêmero, substituir o armazenamento local por banco persistente independente.

## Classificação inicial
- 85–100: aprovado, desde que todas as verificações obrigatórias estejam concluídas.
- 60–84: revisão.
- 0–59: rejeitado.
- Mesmo com pontuação alta, sem empresa, página original, link de candidatura e conteúdo verificados, não há aprovação automática.

## Endpoints
- `GET /health`
- `POST /api/candidates`
- `GET /api/candidates`
- `GET /api/candidates?status=review`
