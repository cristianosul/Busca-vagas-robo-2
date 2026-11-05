Busca Vagas — Robô 2 (Etapa 1 consolidada)
Projeto independente do Robô 1. Esta etapa recebe, organiza e classifica oportunidades em armazenamento separado. Ainda não pesquisa na internet e não publica vagas no Busca Vagas.
Estrutura
app/main.py: API de recepção e consulta.
app/models.py: dados recebidos e evidências internas separadas.
app/repository.py: SQLite isolado, migração e deduplicação.
app/scoring.py: pontuação e regras de triagem.
.env.example: configuração do caminho do banco.
Executar localmente
python -m venv .venv
# Windows:
.venv\\Scripts\\activate
# Linux/macOS:
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
Abra http://127.0.0.1:8000/health e http://127.0.0.1:8000/docs.
Regras de segurança
Banco SQLite local separado (data/staging.db); não conectar ao banco do Robô 1.
A API não aceita campos *_verified enviados pelo cliente. Tentativas são rejeitadas pela validação do modelo.
Evidências só podem ser definidas pelo pipeline interno após checagens reais.
Sem todas as verificações obrigatórias, a oportunidade fica em review; pontuação alta isolada não aprova.
rejected exige motivo explícito de rejeição fornecido pelo pipeline interno.
URL original é normalizada (fragmentos e parâmetros de rastreamento removidos) e protegida por índice único. Inserções concorrentes são serializadas.
publishing_enabled permanece false; nenhum endpoint envia vagas ao site.
Em hospedagem com disco efêmero, configurar banco persistente independente antes de implantar.
Pontuação
Empresa verificada: 25 pontos.
Página original confirmada: 25 pontos.
Link de candidatura verificado: 20 pontos.
Conteúdo consistente: 15 pontos.
Data/validade verificadas: 15 pontos.
Aprovação exige todas as cinco verificações obrigatórias (100 pontos). Na ausência de evidências, a vaga fica em revisão. A pesquisa nacional, verificadores e publicação continuam desativados nesta etapa.
Endpoints
GET /health
POST /api/candidates
GET /api/candidates
GET /api/candidates?status=review
