# Deploys por serviço

Cada serviço tem seu YAML. O discovery é o único que sobe automaticamente no
DGX Spark; classificação e apply ficam manuais até a arquitetura estabilizar.

```bash
# DGX Spark — robô de dados (4 execuções/dia, 10h/12h30/15h/18h30, America/Sao_Paulo)
docker compose --env-file .env -f deploy/postgres.yaml -f deploy/discovery.yaml up -d --build

# Uma rodada avulsa
docker compose --env-file .env -f deploy/postgres.yaml -f deploy/discovery.yaml run --rm discovery \
  python -m src.cli discovery-run --slot first

# Conferir a janela sem buscar
docker compose --env-file .env -f deploy/postgres.yaml -f deploy/discovery.yaml run --rm discovery \
  python -m src.cli discovery-run --slot first --dry-run
```

## Postgres no Spark ( fonte do DBeaver )

O discovery passou a gravar em Postgres no DGX Spark. Assim o DBeaver do notebook
acessa ao vivo por `jdbc:postgresql://10.10.10.2:5432/jobs`. O SQLite antigo
(`data/agent.db`) continua no disco do Spark apenas como origem da migração.

Crie `/home/addam/ia-job-agent/.env` **no Spark** (nunca no repo — ele é público):

```env
POSTGRES_DB=jobs
POSTGRES_USER=jobs
POSTGRES_PASSWORD=<senha-gerada>
AI_JOB_DATABASE_URL=postgresql+psycopg://jobs:<senha-gerada>@postgres:5432/jobs
```

O `rsync` do deploy exclui `.env`, então a senha não é sobrescrita nem versionada.
Como os arquivos `deploy/*.yaml` fazem o Compose usar `deploy/` como diretório de
projeto, passe `--env-file .env` em todo comando para ele encontrar a raiz.

Suba Postgres + discovery juntos para compartilharem a rede:

```bash
docker compose --env-file .env -f deploy/postgres.yaml -f deploy/discovery.yaml up -d --build
```

Copie os dados do SQLite atual do Spark para o Postgres (idempotente):

```bash
docker compose --env-file .env -f deploy/postgres.yaml -f deploy/migration.yaml \
  --profile copy run --rm copy
```

Conferência rápida no container:

```bash
docker compose --env-file .env -f deploy/postgres.yaml -f deploy/discovery.yaml exec -T discovery \
  python -c "from src.storage.database import Repository; r=Repository(); print(r.engine.url.render_as_string(hide_password=True)); print(r.stats()['jobs_total'])"
```

## Migração de dados (legado / JSON )

Se precisar exportar/importar por arquivo (outra máquina, sem rede):

```bash
# 1. exportar da máquina atual
python scripts/migrate_data.py export --source data/agent.db --output data/dgx-snapshot.json

# 2. copiar o JSON para o DGX Spark e importar
python scripts/migrate_data.py import --input data/dgx-snapshot.json \
  --target sqlite:///data/agent.db
```

O import é idempotente: linhas com a mesma chave primária são ignoradas. Para
Postgres, troque `--target` por `postgresql+psycopg://user:pass@host:5432/jobs`.

## DBeaver no notebook

Nova conexão PostgreSQL:

- **Servidor**: `10.10.10.2`
- **Porta**: `5432`
- **Banco**: `jobs`
- **Usuário**: `jobs`
- **Senha**: a mesma de `POSTGRES_PASSWORD` no `.env` do Spark
- **URL JDBC**: `jdbc:postgresql://10.10.10.2:5432/jobs`

Driver: PostgreSQL (padrão do DBeaver, `postgres-jdbc`). Depois de conectar,
a tabela `job_opportunities` deve mostrar as vagas (ex.: 414 após a cópia).

## Banco e seed

Toda subida do contêiner cria as tabelas se elas não existirem (`create_all`).
No SQLite legado também aplica as migrations incrementais de colunas ausentes
(`_ensure_columns` em `src/storage/database.py`). Os seeds idempotentes incluem:

- empresas/portais confiáveis (`BairesDev`, `Strider`) com `is_trusted_portal=1`;
- filtros simples em `trusted_portal_config` (stack, roles, remote, USD, idioma);
- localizações da busca (config.yaml).

Edições feitas pelo operador no JSON do portal **nunca** são sobrescritas.

## CI/CD

`.github/workflows/deploy-discovery.yml` atualiza o robô no DGX Spark a cada push
em `main`. Como o Spark está na LAN (10.10.10.2), o job usa um **runner
self-hosted** (`dgx-spark`, instalado em `~/actions-runner`), não um runner
hospedado. O workflow sincroniza o código com `rsync` preservando `data/` e
`.env`, e roda
`docker compose --env-file .env -f deploy/postgres.yaml -f deploy/discovery.yaml up -d --build`.

Variáveis de ambiente úteis:

- `AI_JOB_CONFIG_DIR` — raiz alternativa de `config/config.yaml` (usada em testes
  de fumaça isolados, sem tocar no banco de produção).
- `AI_JOB_BROWSER_SOURCE` — `""` usa o Chromium embutido no container; o Chrome
  logado continua exclusivo do apply-agent local.
- `AI_JOB_DATABASE_URL` — URL SQLAlchemy do banco. No Spark aponta para o
  Postgres; vazio mantém o SQLite legado (usado nos testes).

Uma varredura completa (16 termos × 18 localidades) leva cerca de 1h no Spark,
com `delay_ms: 1200` e seletores aguardados no estado `attached`.
