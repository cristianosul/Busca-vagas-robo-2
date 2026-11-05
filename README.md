Busca Vagas — Robô 2

Etapa 1 — Recepção, armazenamento e triagem de oportunidades

Projeto independente do Robô 1 do Busca Vagas.

O Robô 2 recebe, organiza, armazena e classifica oportunidades de emprego em um ambiente separado.

Nesta etapa, a pesquisa nacional na internet e a publicação de vagas no site Busca Vagas permanecem desativadas.

1. Estrutura do projeto

app/
├── __init__.py
├── main.py
├── models.py
├── repository.py
└── scoring.py

.env.example
.gitignore
README.md
requirements.txt
test_neon.py

Responsabilidades

- "app/main.py": API de recepção e consulta.
- "app/models.py": modelos de dados recebidos e evidências internas.
- "app/repository.py": armazenamento SQLite ou PostgreSQL, migração e deduplicação.
- "app/scoring.py": pontuação e regras de triagem.
- ".env.example": exemplo das variáveis de configuração.
- "test_neon.py": teste de conexão, recebimento e deduplicação no Neon.

2. Banco de dados

O projeto oferece duas opções de armazenamento:

SQLite — execução local

Quando "DATABASE_URL" não está configurada, o projeto utiliza SQLite, com caminho padrão:

./data/staging.db

O caminho pode ser alterado pela variável "DATABASE_PATH".

Neon PostgreSQL — ambiente remoto

Quando "DATABASE_URL" está configurada, o projeto utiliza PostgreSQL no Neon.

O banco do Robô 2 é independente e não deve compartilhar tabelas, credenciais ou armazenamento com o Robô 1.

Em hospedagens ou automações, configure "DATABASE_URL" como variável de ambiente ou segredo protegido. Nunca publique a string de conexão no código ou no GitHub.

3. Instalação local

Crie um ambiente virtual:

python -m venv .venv

Ative o ambiente.

Windows:

.venv\Scripts\activate

Linux/macOS:

source .venv/bin/activate

Instale as dependências:

pip install -r requirements.txt

Inicie a API:

uvicorn app.main:app --reload

Acesse:

- Saúde da API: http://127.0.0.1:8000/health
- Documentação interativa: http://127.0.0.1:8000/docs

4. Configuração do Neon

Para utilizar o Neon, configure a variável de ambiente "DATABASE_URL" com a string de conexão PostgreSQL do projeto.

No GitHub Actions, o segredo utilizado é:

DATABASE_URL

O valor deve ser cadastrado em:

Settings → Secrets and variables → Actions.

A conexão do Neon já foi testada pelo workflow do GitHub Actions.

5. Teste de integração

O arquivo "test_neon.py" executa verificações no banco:

- Conexão com o Neon.
- Recebimento de uma oportunidade fictícia.
- Bloqueio de registros duplicados.
- Verificação da pontuação e do status de revisão.
- Remoção do registro fictício ao final.

Workflow:

Testar triagem do Robo 2 no Neon

O workflow é iniciado manualmente pela aba Actions do GitHub.

6. Segurança e regras de triagem

- O banco do Robô 1 não deve ser utilizado pelo Robô 2.
- A API não aceita campos "*_verified" enviados pelo cliente.
- Tentativas de enviar evidências externas são rejeitadas pela validação do modelo.
- Evidências só podem ser definidas pelo pipeline interno após verificações reais.
- Sem todas as verificações obrigatórias, a oportunidade permanece em "review".
- Uma pontuação alta isolada não aprova uma oportunidade.
- A rejeição exige motivo explícito fornecido pelo pipeline interno.
- URLs originais são normalizadas para reduzir duplicidades.
- Fragmentos e parâmetros de rastreamento são removidos da URL normalizada.
- Um índice único protege contra registros duplicados.
- Inserções concorrentes são protegidas no armazenamento.

7. Sistema de pontuação

Verificação| Pontos
Empresa verificada| 25
Página original confirmada| 25
Link de candidatura verificado| 20
Conteúdo consistente| 15
Data e validade verificadas| 15
Total| 100

A aprovação exige todas as cinco verificações obrigatórias.

Na ausência de evidências, a oportunidade permanece em revisão.

Uma oportunidade com motivo explícito de rejeição pode receber status "rejected".

8. Endpoints disponíveis

Método| Endpoint| Função
GET| "/health"| Verificar saúde da API
POST| "/api/candidates"| Receber oportunidade
GET| "/api/candidates"| Consultar oportunidades
GET| "/api/candidates?status=review"| Consultar oportunidades em revisão

9. Recursos ainda desativados

Nesta etapa:

- Pesquisa nacional na internet: desativada.
- Verificadores automáticos: desativados.
- Publicação no Busca Vagas: desativada.
- Integração com o Robô 1: não realizada.
- "PUBLISHING_ENABLED=false".

Nenhuma oportunidade é enviada automaticamente ao site.

10. Próximas etapas

Após validar a recepção, o armazenamento e a triagem:

1. Preparar os módulos de pesquisa nacional.
2. Identificar fontes autorizadas de oportunidades.
3. Criar verificadores de empresa, página original, candidatura e validade.
4. Testar a coleta e classificação em ambiente separado.
5. Avaliar uma futura integração com o Busca Vagas somente após testes e autorização.

Objetivo: construir um pesquisador nacional independente, seguro e econômico, mantendo o Robô 1 intacto.
