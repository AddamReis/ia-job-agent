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
em `main`. Como o Spark está na LAN (10.10.10.2), o job usa um **runner
self-hosted** (`dgx-spark`, instalado em `~/actions-runner`), não um runner
hospedado. O workflow sincroniza o código com `rsync` preservando `data/` e
roda `docker compose -f deploy/discovery.yaml up -d --build`.

Variáveis de ambiente úteis:

- `AI_JOB_CONFIG_DIR` — raiz alternativa de `config/config.yaml` (usada em testes
  de fumaça isolados, sem tocar no banco de produção).
- `AI_JOB_BROWSER_SOURCE` — `""` usa o Chromium embutido no container; o Chrome
  logado continua exclusivo do apply-agent local.

Uma varredura completa (16 termos × 18 localidades) leva cerca de 1h no Spark,
com `delay_ms: 1200` e seletores aguardados no estado `attached`.
