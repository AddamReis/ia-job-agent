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
[DGX Spark]  discovery-serve  (4x/dia: 10:00, 12:30, 15:00, 18:30 — America/Sao_Paulo)
                → HTML público → Postgres → DISCOVERED
                → watermark por slot + âncora de tempo congelada
                         |
                         ▼
[DGX Spark]  ClassificationWorker → FastLane (BairesDev/Strider) ou LLM runner → QUALIFIED
                         |
                         ▼  agent_events (outbox persistente, consumer=application)
[máquina local]  ApplicationWorker → revalida recência/remoto/moeda → rota do portal
                         → BrowserApplyAgent.prepare() → PENDING_APPROVAL
                         ▼
[DGX] PWA de revisão (Tailscale) → check humano → APPROVED
                         ▼  agent_events (consumer=apply_submit)
[máquina local]  SubmitWorker → revalida novamente → Submit (última etapa, com CDP)
```

- `reference_time` é capturado **uma vez** no início da execução; todas as queries usam a
  mesma âncora. O cutoff é o mais antigo entre a janela do slot (12h na primeira, 3h nas
  seguintes) + 1h de sobreposição e o watermark da última execução concluída.
- O filtro nativo do LinkedIn continua em 24h (`day`); se a janela passar disso, o
  agente troca automaticamente para `week` (parametrizável).
- **Uma busca por vez**: a tabela `run_locks` grava o dono da rodada e um heartbeat a
  cada 30s. Outra execução (slot seguinte ou disparo manual) fica em standby, consulta
  a trava a cada 2s e corre logo após a liberação. Se o heartbeat parar por 15 min
  (restart no meio da rodada), a trava é recuperada e as execuções/watermarks órfãos
  viram `ABANDONED` — sem dois processos varrendo o LinkedIn ao mesmo tempo.
- Portais confiáveis são **flags na tabela de empresas** (`is_trusted_portal`,
  `trusted_portal_config`), sem código específico por empresa.
- Deploys separados em `deploy/`; veja `deploy/README.md`.

### Rotinas agênticas separadas (primeira versão)

A classificação e a aplicação são **rotinas independentes**, com consulta ao banco como
fonte da verdade e travas próprias em `run_locks` (`classification` e `application`):

```
[DGX] discovery-serve (slot)
  → DiscoveryAgent.run()
  → ClassificationWorker.run()   # DISCOVERED sem qualificação; empresas conhecidas primeiro
      → FastLaneAgent (portal confiável) ou JobQualificationAgent (DGX)
      → job.classified / job.qualified (com job_id)
      → publish_agent_event("application", "job.qualified", job_id)

[DGX] review-serve (PWA via Tailscale Serve)
  → listas: a classificar / recusadas / aprovadas
  → check do operador: revalida remoto, moeda, recência e score
      → o recheck remoto lê título, local e descrição; "80% Remote" e
        "office/home/blend of both" são vetados antes da autorização e no submit
      → ReviewDecisionRow + resolve_approval(APPROVED)
      → publish_agent_event("apply_submit", "review.approved", job_id)

[local, com o mesmo AI_JOB_DATABASE_URL e CDP 127.0.0.1:9222]
  application-serve
  → ApplicationWorker.run()      # QUALIFIED; revalida recência/remoto/moeda antes do CDP
      → claim_agent_events("application") + ack
      → portal known? ApplyRoute (EASY_APPLY / KNOWN_PORTAL / EXTERNAL_SIGNUP)
      → BrowserApplyAgent lê a tela (main + iframes) ANTES de clicar:
        login/OAuth, termos, descrição, formulário, CAPTCHA/senha
      → BrowserApplyAgent.prepare() → PENDING_APPROVAL
      → FIM. Nenhum Submit é feito por este worker.
  apply-submit-serve
  → SubmitWorker.run()           # consome somente o check aprovado na PWA
      → repete revalidações determinísticas
      → BrowserApplyAgent.submit(confirmed=True)
      → CAPTCHA/MFA/verificação => evento FAILED, sem retry/burla
```

- Landing externa (Breezy etc.) sem `input[type=file]`: o agente lê os CTAs visíveis,
  usa a memória de ações + LLM da DGX para escolher o link que abre o formulário real
  (`open_external_apply`) e só registra a decisão como sucesso após o envio concluído.
  Atalhos de login/OAuth (LinkedIn, Indeed, Google) são vetados. O upload é confirmado
  pelo nome do arquivo no input/DOM mesmo quando o portal não tem radio de currículo.

- `classification-run` / `classification-serve`, `application-prepare-run` /
  `application-serve` e `apply-submit-run` / `apply-submit-serve` permitem operar cada
  agente isoladamente. O intervalo dos loops de preparação/classificação é
  `AI_JOB_WORKER_INTERVAL_SECONDS` (padrão 300s); o de envio usa `--interval` (padrão 30s).
- O **outbox** `agent_events` é o handoff entre processos: a DGX classifica e publica o
  `job_id`; o worker local consome, faz `ack` e reencontra o trabalho no banco mesmo após
  restart. Eventos `PROCESSING` órfãos são recuperados após 15 min. Publicar o mesmo
  aviso é idempotente (chave `consumer|event_type|job_id`).
- O barramento em memória complementa o outbox dentro do mesmo processo e apenas **acorda**
  o worker; a fonte da verdade continua sendo o status no banco.
- Prioridade: `is_trusted_portal` + `trusted_portal_config` > `has_account` /
  `known_portals` > demais. Nenhum agente é criado por site; a decisão de rota pertence
  ao agente único.
- **Hermes** (aprendizado) corresponde ao `SitePatternStore` + `ApplyRoute`: o agente
  reutiliza padrões de formulário já aprendidos por domínio e aprende novos após envios
  bem-sucedidos. Isso agora inclui **decisões de UI**: o LLM da DGX interpreta o estado real
  do diálogo e escolhe o botão seguro; a escolha que leva ao envio é persistida por domínio,
  intenção e gatilho e reutilizada na próxima rodada. A decisão nunca inventa rótulos e para
  se a plataforma pedir verificação. **Zaply** (envio simplificado) corresponde à rota
  `EASY_APPLY` do mesmo `BrowserApplyAgent`. Não há integração externa com produtos de mesmo
  nome neste repositório.
- O botão **“Quero me candidatar — automatizar”** da PWA é a própria autorização: aparece
  para toda vaga `QUALIFIED`, sem exigir formulário preparado antes. O clique grava a decisão,
  publica `review.approved` e o `apply-submit-serve` abre o Chrome logado, preenche, captura
  evidências e envia — tudo na mesma rodada. A preparação prévia (`application-serve` /
  `application-prepare-run`) continua disponível para quem quiser revisar as respostas antes,
  mas não é mais pré-requisito. CAPTCHA/MFA/verificação nunca são resolvidos: a rodada para.
- Formulários preenchem **no máximo as 3 experiências mais recentes** (`profile.max_experiences_in_forms`).
  O prompt do LLM recebe só essas três; etapas de “Work experience” não são reescritas e um cartão
  extra vazio é removido interpretando o diálogo aberto por **“Excluir experiência”**: o agente
  consulta a memória local, pede ao LLM da DGX o botão seguro quando não há memória e aprende
  a decisão após o envio bem-sucedido. Na prática ele chega a **“Remover”**, sem criar uma quarta
  experiência; o botão “Avaliar” leva à revisão final antes do envio.

Operação local: as rotinas de Apply fazem **preflight do CDP** e ficam em standby (heartbeat no
log) até o Chrome já logado responder em `http://127.0.0.1:9222/json/version`. Elas reaproveitam
o perfil/cookies existentes — nunca criam perfil, exportam cookies, maximizam ou fecham a janela.
Se o CDP não responder em `cdp_wait_seconds` (padrão 1800s), nenhuma vaga é alterada.

```powershell
$env:AI_JOB_DATABASE_URL = 'postgresql+psycopg://jobs:<senha>@10.10.10.2:5433/jobs'
# Necessário: consome a autorização da PWA e faz preparar + enviar no Chrome logado.
.\.venv\Scripts\python -m src.cli apply-submit-serve --interval 30
# Opcional: apenas preenche/forma o PENDING_APPROVAL para revisão prévia das respostas.
.\.venv\Scripts\python -m src.cli application-serve
```

Os prints por step e a confirmação ficam em `data/evidence/<job_id>/` em **WebP** (fallback
JPEG) com qualidade configurável; o Postgres guarda apenas caminho relativo e hash — sem BLOB,
sem gzip extra sobre imagem já comprimida. A retenção é de **30 dias** (`evidence_retention_days`)
com limpeza no envio, limpeza lazy ao abrir o detalhe/servir a evidência na PWA e varredura
diária automática no servidor da PWA (`evidence-cleanup-run` / `evidence-cleanup-serve` também
estão disponíveis via CLI).

A PWA de revisão roda na DGX e é publicada somente na tailnet em
`https://dgx-spark.tail040a06.ts.net/` (Tailscale Serve → `127.0.0.1:8765`).
O dropdown de listas segue esta ordem: **Aguardando Aprovação** (padrão; depende do check
humano), **Enviados** (automação concluída), **Recusado**, **Erros** (falha de automação) e
**Todas** (última opção, apenas filtro). Erros têm precedência sobre enviado; enviado tem
precedência sobre recusado. Os contadores de cada lista aparecem nas opções.

A busca textual ao lado do dropdown filtra título, empresa, local, descrição, resumo da análise,
motivos e skills — tudo no banco, via `ILIKE` sobre a coluna JSON `data`. A paginação é fixa em
**10 vagas por página** (`LIMIT/OFFSET` no SQL) e a ordenação usa a hora de inclusão
(`created_at`): **Mais recentes** (`newer`, padrão) ou **Mais antigos** (`older`). Os contadores
das abas acompanham o texto digitado. A API correspondente é
`GET /api/jobs?bucket=&q=&sort=&page=`; `sort` aceita `newer`/`older` e `page` começa em 1.

- **Playwright + Chrome real**: fonte padrão de descoberta. Lê o `ItemList` em JSON-LD da busca
  pública (ID real da vaga, título, empresa, local) e a descrição da página `jobs/view/<id>`.
  Sem login, sem API privada, sem UA falso e sem evasão. Ritmo conservador (`delay_ms: 1200`).
- **ADB + Appium/UiAutomator2**: usados **somente no celular**, para descobrir o botão de Easy
  Apply e aplicar na vaga aprovada, abrindo a URL pública `https://www.linkedin.com/jobs/view/<id>`.
  Nunca são usados como fonte de descoberta (`browser.fallback_to_android: false`).
- **LLM**: somente dois endpoints locais na DGX Spark, ambos via API compatível com OpenAI,
  com `api_key: vllm` e timeout 300s. `llm.primary` (`dgx`, `http://10.10.10.2:8011/v1`)
  usa o denso `qwen3.8-27b-nvfp4` para Apply/respostas e desenvolvimento; `llm.runner`
  (`dgx_runner`, `http://10.10.10.2:8012/v1`) usa o MoE NVFP4 menor `qwen36_nvfp4` para
  extração, elegibilidade, supressão e classificação. `llm.fallback: null`: nunca há
  Ollama, LM Studio ou API paga. O modo "thinking" do Qwen3 é desligado
  (`enable_thinking=false`) para evitar respostas presas.
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
  **até 6 horas**. `expired_after_hours: 96`: só vira `EXPIRED` após **4 dias** (janela separada da
  descoberta); entre 6h e 96h a vaga ainda é classificada normalmente. A fila de candidaturas e o
  preflight de envio revalidam a mesma idade.
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

Para portais em `application.portal_auth`, o próprio agente faz login — você não precisa abrir
site por site. Existem dois modos:

- `auth_mode: "google"` (BairesDev, Alignerr): o agente lê a tela, aceita o checkbox de termos
  quando `terms_checkbox_required: true`, clica no botão Google configurado, escolhe a conta
  `oauth_account_email` e conclui o consentimento reaproveitando a sessão já autenticada no
  Chrome CDP. Não existe senha desses portais e nada precisa ser cadastrado no cofre. Se o Google
  pedir senha ou verificação, o agente fecha esse desafio e tenta **uma vez** o rótulo de
  `oauth_fallback_labels` (ex.: "Continue with LinkedIn" na Alignerr), também sem digitar nada.
  Se o LinkedIn pedir senha/CAPTCHA/MFA, ele para e reporta.
- `auth_mode: "password"` (outros portais): usuário/senha ficam no Windows Credential Manager.
  Cadastre uma vez:

```powershell
python -m src.cli portal-auth --set --domain portal.exemplo.com --username SEU_USUARIO
```

A senha é pedida com eco desligado e nunca vai para config, banco ou log. Se o portal ou o Google
pedirem CAPTCHA/MFA/verificação/senha, o agente para sem interagir.

O **PIN de desbloqueio da máquina Windows** fica apenas no Windows Credential Manager, no mesmo
cofre usado pelas credenciais de portal:

```powershell
python -m src.cli machine-pin --set     # pede o PIN com eco desligado
python -m src.cli machine-pin           # mostra só "cadastrado"/"ausente"
python -m src.cli machine-pin --delete
```

Esse valor **nunca** é enviado à DGX, gravado em `config.yaml`, banco, evidência ou log. A DGX não
precisa dele: o worker remoto apenas conversa com o Chrome logado em `127.0.0.1:9222` pelo túnel
SSH reverso. Enquanto a sessão Windows estiver ativa (mesmo bloqueada), o Chrome e o túnel seguem
disponíveis e a automação roda normalmente. Se a máquina for suspensa/desligada, o túnel cai e é
preciso religar a máquina localmente — o PIN não é uma forma de burlar MFA ou CAPTCHA de portal.

O `APPROVED` humano antes de
cada Submit continua obrigatório. Landing externa sem formulário é percorrida no máximo quatro
CTAs, sempre relendo a tela; a mesma etiqueta em telas diferentes (ex.: RecruitCRM) é permitida,
mas atalhos sociais, banners de cookie e navegação são vetados. O diálogo pós-envio com testes
internos da plataforma é tratado
como informação: em portal com `assessment_status: completed` (ex.: BairesDev) ele não é confundido
com dados da candidatura nem reexecutado.

Se o portal mostrar sinal de candidatura já enviada (`already_applied_markers`, ex.: "Ver
solicitud" na BairesDev), o agente registra a candidatura existente, marca a vaga como
`SUBMITTED` e **não** cria aprovação, anexa CV ou clica em Submit — evitando duplicação.

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
(96h/4 dias; nunca é excluída do banco). Ao final da
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

## Auditoria BairesDev (todas as vagas recebidas)

Foram auditadas **22 vagas** BairesDev no banco remoto:

- **19 `REJECTED_BY_FILTER`** — Kotlin, Go, Django/Python, Node.js, PHP, Ruby, Java,
  AI/Next, ASP Classic/VB, Azure IoT e similares. Títulos sem `.NET/C#` como core, fora do
  perfil. Um caso com `10+ anos` e liderança explícita também foi corretamente recusado.
- **3 `SUBMITTED`** — todas são a **mesma vaga** (`Senior Microsoft Teams & .NET Developer`,
  job 298508) em localizações diferentes (Brazil, Lima, Guayas). O portal exibia
  `Ver solicitud`: o agente **reconheceu candidatura já existente**, registrou a evidência e
  **não clicou em Submit** — por isso não há `approval_requests` para elas. O status
  `SUBMITTED` aqui significa “candidatura preexistente detectada”, não envio novo.
- A vaga irmã sem descrição (Chile) foi recusada por falta de evidência; está coerente com
  a regra de não classificar sem dados para validar remoto/moeda/idioma.

O fast lane permaneceu coerente: `.NET/C#` no título protege; `lead the creation` e
`key technical driver` **não** são liderança de pessoas; `5+ anos` cabe no perfil (7 anos).
Nuances adicionadas ao perfil/prompt: nuvem (AWS/GCP/Microsoft) como experiência
complementar, arquitetura de sistemas como ponto forte, liderança de pessoas/projetos e
Scrum como experiência válida — sem transformar o candidato em tech lead.

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
