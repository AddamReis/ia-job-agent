# IA Job Agent

Agente local de pesquisa e candidaturas internacionais. A **descoberta e a qualificação** rodam
em **páginas públicas do LinkedIn via browser** (HTML + JSON-LD, sem login e sem API privada);
o **dispositivo Android real** entra somente na etapa de candidatura, depois da aprovação humana.
Toda a inteligência roda no LLM local da DGX Spark na LAN. Não há dependência de APIs pagas
de LLM e esta máquina não usa LM Studio/Ollama.

> **Escopo aprovado:** Easy Apply dentro do app do LinkedIn. A automação não oculta sua natureza,
> não falsifica telemetria, não burla CAPTCHA/MFA e para imediatamente se a plataforma solicitar
> verificação. Termos legais, visto/sponsorship, autorização de trabalho e autenticações são
> **sempre** aprovados pelo usuário.

## Arquitetura hexagonal (serviços independentes)

```
src/shared/     → config, domínio, portas, seed, janela de descoberta
src/adapters/  → storage, LLM (DGX), web (público/CDP), scheduler local
src/agents/    → discovery | classification | apply | fast_lane (cada um com entrypoint)
```

```
[DGX Spark]  discovery-agent  (4x/dia: 10:00, 12:30, 15:00, 18:30 — America/Sao_Paulo)
                → HTML público → SQLite → DISCOVERED
                → watermark por slot + âncora de tempo congelada
                         |
                         ▼
[local, por ora]  classification-agent → DGX → memória → QUALIFIED
                         |
                         ▼
[local]  apply-agent / fast lane (BairesDev, Strider) → CDP logado → APPROVED → Submit
```

- `reference_time` é capturado **uma vez** no início da execução; todas as queries usam a
  mesma âncora. O cutoff é o mais antigo entre a janela do slot (12h na primeira, 3h nas
  seguintes) + 1h de sobreposição e o watermark da última execução concluída.
- O filtro nativo do LinkedIn continua em 24h (`day`); se a janela passar disso, o
  agente troca automaticamente para `week` (parametrizável).
- Portais confiáveis são **flags na tabela de empresas** (`is_trusted_portal`,
  `trusted_portal_config`), sem código específico por empresa.
- Deploys separados em `deploy/`; veja `deploy/README.md`.

- **Playwright + Chrome real**: fonte padrão de descoberta. Lê o `ItemList` em JSON-LD da busca
  pública (ID real da vaga, título, empresa, local) e a descrição da página `jobs/view/<id>`.
  Sem login, sem API privada, sem UA falso e sem evasão. Ritmo conservador (`delay_ms: 1200`).
- **ADB + Appium/UiAutomator2**: usados **somente no celular**, para descobrir o botão de Easy
  Apply e aplicar na vaga aprovada, abrindo a URL pública `https://www.linkedin.com/jobs/view/<id>`.
  Nunca são usados como fonte de descoberta (`browser.fallback_to_android: false`).
- **LLM**: somente DGX Spark `qwen3.8-27b-nvfp4` via API compatível com OpenAI
  (`llm.primary: dgx`, `llm.fallback: null`, timeout 300s). O modo "thinking" do Qwen3 é
  desligado (`enable_thinking=false`) para evitar respostas presas.
- **Perfil como fonte de verdade**: `CandidateProfile` derivado do CV real
  (`Addam dos Reis Oliveira - Software Engineer.pdf`, 11/11/2026). Toda resposta carrega origem;
  sem evidência, o campo vira `REQUIRES_HUMAN_ACTION`.

## Instalação

```powershell
cd C:\projects\local-ai-job-agent
# O browser usa o Google Chrome já instalado (browser.source: chrome).
# Se preferir o Chromium gerenciado: .\.venv\Scripts\python -m playwright install chromium
npm install -g appium
appium driver install uiautomator2
```

Copie `config/config.example.yaml` para `config/config.yaml` e ajuste se necessário
(`agent init` faz isso). O ADB está em
`C:\Users\addam\AppData\Local\Android\Sdk\platform-tools\adb.exe`.

### Dispositivo

1. Ative **Opções do desenvolvedor** e **Depuração USB**.
2. Conecte o celular via USB e aceite a autorização RSA/ADB.
3. A senha de desbloqueio está no **Windows Credential Manager** (`keyring`), nunca em arquivo.
   Para trocar: `agent device --set-unlock`.

### Política de vagas

Configurada em `config/config.yaml` (`search` + `qualification.reject`):

- **16 termos ativos** (6 variações LATAM + `.NET Engineer`, `.NET Developer`, `DotNet Engineer`,
  `DotNet Developer`, `Software Engineer`, `Software Developer`, `Backend Engineer`,
  `Backend Developer`, `Full Stack .NET Engineer`, `Full Stack .NET Developer`), sem exigir
  `Senior`/`Lead` no título. A sobreposição entre termos semelhantes é medida pelo dedupe por ID
  real: cada termo que não traz vaga nova é candidato a descarte.
- `remote_only: true`: somente vagas **100% remotas**; híbrido/presencial são rejeitadas.
- Moeda é **critério**, mas não entra no score. Só USD/EUR são elegíveis (`search.currencies`);
  o sinal (`currency_signal` + `multi_currency`) continua salvo para auditoria. BRL/outra moeda
  explícita reprova. Vaga do Brasil ou em português sem pagamento em USD/EUR explícito reprova
  (ex.: empresa que anuncia no Brasil mas paga em USD só passa com essa informação na descrição).
  Vaga internacional sem moeda informada é presumida USD/EUR.
- `allow_national_remote: true`: vagas nacionais (ex.: empresa sediada em SP) são aceitas se
  cumprirem **todos** os critérios acima. Países nacionais em `national_countries`.
- Comparação de tecnologias normalizada: `.NET`, `DotNet`, `Dot Net`, `.NET Core` e `.Net Framework`
  viram `net`; `C#` e `C Sharp` viram `c`. Buscas incluem variantes `DotNet Engineer`/`DotNet Developer`.
- `recency_hours: 6` + `require_explicit_recency: true`: descoberta só com idade explícita de
  **até 6 horas**. `expired_after_hours: 24`: só vira `EXPIRED` após **24 horas** (janela separada,
  mais larga na fase de testes); entre 6h e 24h a vaga ainda é classificada normalmente. A fila de
  candidaturas revalida a idade antes de qualquer envio.
- A busca pública usa `f_TPR=r86400` apenas para reduzir volume; a autoridade é a idade explícita
  lida do `<time>` de cada card (`"16 minutes ago"`, `"2 hours ago"`). O ID real vem do JSON-LD e
  a descrição da página `jobs/view/<id>` — o dedupe é feito por esse ID, nunca por hash de texto.

### Ordem do pipeline de candidaturas

```
1. search (browser)  → 16 termos, 6h, ID real, descrição pública, 100% remoto, moeda USD/EUR
2. qualification     → descrição × requisitos × perfil (mín. 50% das tecnologias pedidas)
3. classificação     → região + easy_apply | known_portal | external_portal
4. ordenação         → América do Norte > Austrália > Nova Zelândia > Europa > Brasil
                       (dentro da região: simplificada → portal conhecido → externo,
                        depois mais recente e maior score)
5. prepare + perguntas → CV + LinkedIn + doc local de domínios (tech_domains_doc)
6. aprovação humana  → submit (celular via Appium, abrindo /jobs/view/<id>)
7. registro no banco → somente após confirmação de sucesso do portal
```

`application.known_portals` lista os sites onde você **já tem cadastro ativo** — edite-a com a
sua lista real (BairesDev/Strider são apenas exemplos). A ordem é
`application.channel_priority`. O envio em portais externos com confirmação de cadastro é a
etapa seguinte; o registro de candidatura só acontece quando o portal confirma o sucesso.

Para portais em `application.portal_auth`, o próprio agente faz login com usuário/senha salvas no
Windows Credential Manager — você não precisa abrir site por site. Cadastre uma vez:

```powershell
python -m src.cli portal-auth --set --domain applicants.bairesdev.com --username SEU_USUARIO
```

A senha é pedida com eco desligado e nunca vai para config, banco ou log. Se o portal pedir
CAPTCHA/MFA/verificação, o agente para sem interagir. O `APPROVED` humano antes de cada Submit
continua obrigatório. O diálogo pós-envio com testes internos da plataforma é tratado como
informação: em portal com `assessment_status: completed` (ex.: BairesDev) ele não é confundido
com dados da candidatura nem reexecutado.

Importante: **a API pública do LinkedIn não informa se uma vaga é Easy Apply**. Por isso a
coluna `easy_apply` é tri-estado: `NULL` = ainda não analisada, `True` = botão de candidatura
simplificada visto na tela do celular, `False` = visto e ausente. A descoberta pública nunca
preenche essa coluna. O agente abre cada vaga qualificada **pelo ID real, uma por vez**, no app
logado, detecta o botão e grava `True`/`False` antes de qualquer candidatura. Nada é inferido de
marcador de cartão, URL, empresa ou descrição.

### Busca e qualificação em paralelo

A busca usa o **browser público** (Playwright/Chrome) e a qualificação usa a **LLM local**
(DGX Spark) + SQLite — recursos independentes. Por isso a qualificação roda numa thread em
background que consome as vagas `DISCOVERED` conforme a busca as salva, sem esperar as 12
consultas terminarem. O log mostra as duas frentes ao vivo:

```
Busca 1/12 — novas=16 acumulado=16
Analisadas 1/352 — Acme · .NET Engineer [APPLY]
Busca 2/12 — novas=1 acumulado=17
Analisadas 2/352 — Beta · C# Developer [SKIP]
...
```

O filtro nativo `f_TPR=r86400` é best-effort: ele só reduz o volume. A **autoridade é a idade
explícita** lida do `<time>` de cada vaga (`"16 minutes ago"`, `"2 hours ago"` etc.). Vagas sem
idade explicitada ou acima de `search.recency_hours` (6h) são descartadas na descoberta, antes
mesmo da chamada à LLM. Na reclassificação, só vira `EXPIRED` após `search.expired_after_hours`
(24h; nunca é excluída do banco). Ao final da
busca, a thread drena o que sobrou; pause/stop também a interrompem. A descoberta é somente HTML
público — nunca cai para o Android (`browser.fallback_to_android: false`).

## Comandos

| Comando | Finalidade |
|---|---|
| `agent status` | Estado do agente, device, LLM, perfil e contadores |
| `agent device` | Dispositivo, LinkedIn instalado, senha de desbloqueio |
| `agent device --unlock` | Testa o desbloqueio via ADB |
| `agent device-pending` | Vagas Easy Apply aguardando a conexão do celular |
| `agent device-apply` | Processa a fila no celular conectado (PDF, respostas e envio) |
| `agent device-apply --watch` | Fica aguardando o celular e processa a fila automaticamente |
| `agent dependency-done <id>` | Dá o check no banco após vídeo/candidatura feita por você |
| `agent profile` | Perfil estruturado e afirmações não verificadas |
| `agent profile-refresh-linkedin` | Atualiza o perfil com as skills atuais do LinkedIn logado (somente leitura) |
| `agent reclassify [--limit N] [--statuses ...] [--ids ...] [--trace-id ...] [--all] [--ignore-recency]` | Reanalisa vagas salvas com as regras atuais, sem candidatar; `--ignore-recency` audita o histórico ignorando as 6h |
| `agent normalize-locations` | Preenche `País - localização da vaga` nas vagas já salvas (sem reanalisar) |
| `agent link-companies` | Vincula `company_id` nas vagas antigas (sem reanalisar) |
| `agent sanitize-restriction-details` | Reescreve detalhes semânticos antigos sem repetir o perfil |
| `agent search [--source browser\|android] [--no-qualify]` | Pesquisa pública (padrão; `--source android` força o fallback) e qualifica em paralelo; `--no-qualify` só descobre vagas |
| `agent jobs --status QUALIFIED` | Tabela com região, canal, recência e indicador de moeda |
| `agent progress` | Progresso da busca (inclui o trace ID da execução) |
| `agent apply --max-applications 10` | Prepara candidaturas (a qualificação já rodou na busca) |
| `agent run --max-applications 10` | Fluxo completo com painel em tempo real |
| `agent report` | Relatório do SQLite |
| `agent pause` / `resume` / `stop` | Controle da execução |
| `agent gmail-auth` | OAuth do Gmail (leitura das caixas de entrada + envio) |
| `agent reminder [--force]` | Envia manualmente o lembrete diário consolidado |
| `agent trace <trace_id>` | Rastreia vagas, candidaturas, dependências e e-mails de uma execução |
| `agent doctor` | Diagnóstico de ADB e LLM |
| `agent company list` | Lista empresas e supressões temporárias ativas |
| `agent company check <nome>` | Confirma o padrão de localização/visto em outras vagas da empresa no LinkedIn logado |
| `agent company suppress <nome>` | Marca a empresa por 30 dias somente após confirmação |
| `agent company clear <nome>` | Cancela uma supressão antes do prazo |

## Supressão temporária de empresas

Não é blacklist e não filtra a busca. A análise só sinaliza empresas quando há restrição de
**localização, residência, visto ou autorização de trabalho**; requisito técnico/idioma nunca gera
flag. Antes de marcar, o agente confirma o mesmo padrão em outras vagas da página da empresa no
LinkedIn logado. A segunda ponta acontece na busca pública: vagas de empresas suprimidas são
descartadas **antes** do `upsert`. A supressão expira automaticamente (padrão: 30 dias).

A reclassificação de dados já existentes ignora supressões: se a empresa estivesse suprimida, a vaga
não estaria na tabela. Use `agent reclassify --trace-id <id>` para reanalisar somente as vagas de uma
execução (o "ID de potência"), `--ids <uuid,...>` para IDs específicos ou `--all` para incluir
rejeitadas e vencidas. Nenhuma dessas rotas envia candidatura.

A localização é gravada como `País - informação da vaga` (ex.: `Brasil - Manaus, AM`,
`Estados Unidos - Bethesda, MD`, `Portugal - Lisbon`) já na descoberta. Vagas antigas recebem o
campo na qualificação; para preencher sem reanalisar, use `agent normalize-locations`.

Empresa e vaga são vinculadas no momento da gravação (`upsert_job`/`update_job`): o `company_id`
é criado/buscado pelo nome normalizado e gravado tanto na coluna quanto no JSON da vaga. Vagas
antigas são ligadas com `agent link-companies`. A supressão e os dados já preenchidos da empresa
são preservados.

## E-mails e rastreabilidade

- **Resumo por execução**: enviado ao final de cada `agent run`/`search`/`apply`, com três tabelas
  HTML — resultado por termo/localidade (encontradas, elegíveis, cadastro automático, falhas e
  pendentes), pendências de ação humana e falhas/não elegíveis.
- **Lembrete diário**: **um único e-mail por dia**, no primeiro job a partir de
  `gmail.reminder_hour` (padrão `18`). Reúne pendências e erros em duas tabelas, destaca prazos
  próximos/vencidos e nunca envia um e-mail por vaga. Prazos não antecipam a cadência diária.
  Para garantir o envio mesmo sem rodar a busca às 18h, agende `agent reminder` no Agendador de
  Tarefas do Windows (por exemplo, 18:05). O comando respeita a cadência e é idempotente.
- **Trace ID**: o `run_id` da execução é usado como trace ID em logs, `status.json`, vagas,
  candidaturas, dependências, aprovações, histórico e no cabeçalho `X-Job-Agent-Trace-Id` dos
  e-mails. Use `agent trace <id>` para ver a evolução daquela execução, incluindo os e-mails
  enviados e os IDs de mensagem do Gmail.

## Candidatura simplificada (Easy Apply)

A página pública do LinkedIn não permite Easy Apply: é obrigatório estar logado na plataforma.
Por isso o fluxo é em duas etapas:

1. **Sem celular conectado**: vagas `easy_apply` aprovadas recebem o status `WAITING_DEVICE`.
   A aprovação humana é coletada e persistida na hora (`ApprovalRequest` `APPROVED`), e a vaga
   fica pronta — sem URL sintética e sem envio.
2. **Ao conectar o celular**: `agent device-apply` (ou `--watch`) percorre a fila, abre cada vaga
   pelo **ID real** via deep link (`https://www.linkedin.com/jobs/view/<id>`), sobe o PDF do CV,
   preenche as respostas possíveis e envia. Aprovação ausente não envia; camera/vídeo/visto viram
   dependências humanas pendentes, nunca são marcadas como feitas automaticamente.

- `agent device-pending` mostra quantas vagas e quais estão aguardando o celular (também aparece
  no `agent status` como `jobs_waiting_device` e nos dois e-mails).
- Falhas transitórias voltam para `WAITING_DEVICE` para nova tentativa; limites diários e
  pause/stop continuam valendo.
- Depois de gravar o vídeo/fazer a candidatura no app, registre o check com
  `agent dependency-done <id>` (ou `--opportunity <id> --kind video_self_intro`). O comando
  aceita `--note` curto e é idempotente.

## Segurança

- Nenhuma senha, token, cookie ou código em `config.yaml`, banco ou logs.
- Logs com redação automática (`src/core/logging.py`).
- Limite diário persistente em `DailyLimits` e dedupe por ID real do LinkedIn (`external_id`).
- Ações sensíveis sempre exigem confirmação humana.
- O agente para em CAPTCHA, MFA, bloqueio ou verificação da plataforma.

## Testes

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python tests\smoke_qualification.py   # usa LLM real e um PDF de vaga
```
