# Deploys por serviço

Cada serviço tem seu YAML. O discovery é o único que sobe automaticamente no
DGX Spark; classificação e apply ficam manuais até a arquitetura estabilizar.

```bash
# DGX Spark — robô de dados (4 execuções/dia, 10h/12h30/15h/18h30, America/Sao_Paulo)
docker compose -f deploy/discovery.yaml up -d --build

# Uma rodada avulsa
docker compose -f deploy/discovery.yaml run --rm discovery \
  python -m src.cli discovery-run --slot first

# Conferir a janela sem buscar
docker compose -f deploy/discovery.yaml run --rm discovery \
  python -m src.cli discovery-run --slot first --dry-run
```

## Migração de dados

```bash
# 1. exportar da máquina atual
python scripts/migrate_data.py export --source data/agent.db --output data/dgx-snapshot.json

# 2. copiar o JSON para o DGX Spark e importar
python scripts/migrate_data.py import --input data/dgx-snapshot.json \
  --target sqlite:///data/agent.db
```

O import é idempotente: linhas com a mesma chave primária são ignoradas. Para
Postgres, troque `--target` por `postgresql+psycopg://user:pass@host:5432/jobs`.

## Banco e seed

Toda subida do contêiner cria o banco se ele não existir e aplica as migrations
de colunas ausentes (`_ensure_columns` em `src/storage/database.py`) e os seeds
idempotentes de:

- empresas/portais confiáveis (`BairesDev`, `Strider`) com `is_trusted_portal=1`;
- filtros simples em `trusted_portal_config` (stack, roles, remote, USD, idioma);
- localizações da busca (config.yaml).

Edições feitas pelo operador no JSON do portal **nunca** são sobrescritas.

## CI/CD

`.github/workflows/deploy-discovery.yml` atualiza o robô no DGX Spark a cada push
em `main`. Segredos necessários: `DGX_HOST`, `DGX_USER`, `DGX_SSH_KEY`,
`DGX_DEPLOY_PATH`.
