Busca Vagas — Robô 2
Pesquisador nacional independente de oportunidades e fontes de emprego no Brasil. O Robô 2 mantém banco, credenciais, execução e armazenamento separados do Robô 1.
Estado do projeto
Recepção, persistência, deduplicação e triagem: implementadas.
Descoberta nacional por categorias: implementada.
Descoberta por regiões e unidades federativas: implementada.
Catálogo municipal oficial do IBGE e divisão em lotes: implementados.
Descoberta de páginas de carreiras em domínios oficiais: implementada com validação HTTPS, DNS público, limites de resposta e proteção contra redirecionamentos inseguros.
Pesquisa automática agendada: não ativada por estes endpoints.
Publicação no site Busca Vagas: desativada.
Integração com o Robô 1: não realizada.
Estrutura principal
app/
├── main.py
├── models.py
├── repository.py
├── scoring.py
├── search_engine.py
├── company_discovery.py
├── national_discovery.py
├── source_registry.py
└── municipality_scheduler.py

tests/
├── test_national_discovery.py
├── test_discovery_api.py
├── test_ibge_live.py
├── test_company_discovery.py
└── test_municipality_scheduler.py
Instalação local
python -m venv .venv
Windows:
.venv\Scripts\activate
Linux/macOS:
source .venv/bin/activate
Instale as dependências e inicie a API:
pip install -r requirements.txt
uvicorn app.main:app --reload
Documentação interativa: http://127.0.0.1:8000/docs.
Configuração
Variáveis de ambiente:
DATABASE_URL: conexão PostgreSQL/Neon do Robô 2. Se vazia, o repositório utiliza SQLite conforme a configuração local.
SOURCE_ADMIN_TOKEN: token obrigatório para endpoints administrativos e de descoberta.
TAVILY_API_KEY: chave do provedor de pesquisa web. Não colocar a chave no código ou no repositório.
Envie o token no cabeçalho X-Admin-Token. Não compartilhe credenciais em issues, logs públicos ou capturas de tela.
Endpoints de descoberta
Todos os endpoints abaixo exigem X-Admin-Token.
Pesquisa nacional
POST /api/discovery/national
Parâmetros:
categories: categoria repetível e opcional. Valores aceitos: companies, agencies, temporary_work, headhunters, ats, regional_public_sources.
limit: resultados por consulta, de 1 a 20; padrão 10.
include_geography: quando true, acrescenta consultas para as cinco regiões e 27 UFs; padrão false.
Exemplo:
POST /api/discovery/national?categories=companies&categories=ats&include_geography=true&limit=5
A resposta separa results (URLs únicas) de matches (cada ocorrência com categoria, consulta e localização), preservando a rastreabilidade de resultados encontrados por mais de uma consulta. Falhas parciais aparecem em errors.
Pesquisa por empresa
POST /api/discovery/company?company_name=Nome%20da%20Empresa&limit=10
Executa consultas para localizar páginas de carreiras e oportunidades associadas à empresa. A pesquisa não cadastra automaticamente uma fonte.
Inspeção do site oficial
POST /api/discovery/company/career-pages?company_name=Nome%20da%20Empresa&official_site_url=https%3A%2F%2Fempresa.example
Inspeciona o endereço HTTPS informado e procura links de carreiras dentro do domínio validado. O mecanismo aplica validação DNS/IP público, limites de tempo e tamanho e controle de redirecionamentos. A operação não cadastra a fonte.
Pesquisa avulsa
POST /api/discovery/search?query=texto&limit=10
Executa uma consulta web individual sem persistência automática.
Cobertura geográfica
O catálogo municipal é obtido da API pública de localidades do IBGE. O carregamento valida código municipal, nome e UF, elimina códigos repetidos e mantém o catálogo em cache durante a execução do processo.
Os municípios são ordenados por UF, nome e código e divididos em lotes de até 60. O worker deve reivindicar e processar um município por vez com search_municipality; search_municipalities executa todas as consultas do lote e não deve ser usado pelo worker unitário.
A cobertura está organizada em cinco regiões e 27 unidades federativas. A lista de UFs e a associação de cada UF à região correspondente possuem testes locais. O workflow manual Verificar catalogo nacional do IBGE consulta o catálogo real para verificar quantidade nacional e presença de todas as UFs.
O registro de lotes municipais não inicia a pesquisa. Os endpoints administrativos de lote permanecem separados do worker.
Scheduler municipal
A API administra e monitora lotes. O worker é um processo separado, acionado manualmente ou por agendamento externo. O worker deve:
reivindicar um município por vez;
executar a pesquisa daquele município;
renovar o heartbeat durante a execução;
concluir ou falhar usando o lease_token recebido;
respeitar o máximo de três tentativas e a recuperação de execuções interrompidas.
A API não inicia pesquisas em segundo plano como efeito de uma requisição HTTP.
Segurança e triagem
Fontes novas permanecem desativadas por padrão.
A ativação direta de fontes continua bloqueada.
Descoberta não grava candidatos ou fontes automaticamente.
Não há publicação automática de vagas.
Não há integração com o Robô 1.
A aprovação continua exigindo as cinco evidências obrigatórias e a pontuação aprovada; ausência de evidência mantém a oportunidade em revisão.
A pontuação permanece: empresa verificada (25), página original (25), link de candidatura (20), conteúdo (15) e data/validade (15).
Testes
Suíte local:
python -m pytest -q
Os testes usam provedores simulados e banco SQLite isolado. Eles não comprovam disponibilidade do Tavily nem concorrência real do PostgreSQL/Neon.
Verificação externa do catálogo IBGE (manual no GitHub Actions):
RUN_LIVE_IBGE_TESTS=1 python -m pytest -q tests/test_ibge_live.py
Workflows
Suite completa de testes do Robo 2: suíte local automatizada.
Verificar catalogo nacional do IBGE: teste externo manual contra a API pública do IBGE.
Workflows de integração Neon e Tavily: executados separadamente e dependem dos respectivos segredos.
Fora do escopo desta etapa
Pesquisa contínua automática e agendamento produtivo.
Extração e verificação completa dos cinco critérios de cada vaga (Etapa C).
Publicação no site Busca Vagas.
Compartilhamento de banco ou código operacional com o Robô 1.
