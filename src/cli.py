"""CLI principal: agent <comando>."""
from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .adapters.scheduler import LocalScheduler
from .agents.agent_runtime import run_worker_loop
from .agents.application_worker import ApplicationWorker
from .agents.browser_apply import BrowserApplyAgent
from .agents.classification_worker import ClassificationWorker
from .agents.submit_worker import SubmitWorker
from .agents.company_suppression import CompanySuppressionAgent
from .agents.discovery import DiscoveryAgent
from .agents.fast_lane import FastLaneAgent
from .agents.orchestrator import Orchestrator
from .agents.qualification import job_side_language_reason
from .core.config import Settings
from .core.errors import AgentError, BlockedByPlatformError, HumanActionRequired, ValidationError
from .core.events import Event, EventBus
from .core.geo import normalize_location
from .core.state import JobStatus
from .dashboard import Dashboard
from .execution.web.portal_credentials import PortalCredentialStore
from .llm.factory import LLMRouter
from .storage.database import Repository

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

app = typer.Typer(help="Agente local de candidaturas com dispositivo Android real")
console = Console()


def _settings() -> Settings:
    return Settings.load()


def _orchestrator(
    bus: EventBus | None = None, settings: Settings | None = None
) -> Orchestrator:
    return Orchestrator(settings or _settings(), bus or EventBus())


def _classification_worker(orchestrator: Orchestrator) -> ClassificationWorker:
    return ClassificationWorker(
        orchestrator.repo,
        orchestrator.settings,
        orchestrator.qualification,
        orchestrator.bus,
    )


def _application_worker(orchestrator: Orchestrator) -> ApplicationWorker:
    settings = orchestrator.settings
    agent = BrowserApplyAgent(
        orchestrator.repo,
        settings,
        orchestrator.profile,
        orchestrator.resolver,
        orchestrator.gate,
        orchestrator.bus,
        cdp_url=settings.browser.cdp_url,
        memory=orchestrator.qualification_memory,
    )
    return ApplicationWorker(
        orchestrator.repo, settings, agent, orchestrator.bus
    )


def _submit_worker(orchestrator: Orchestrator) -> SubmitWorker:
    settings = orchestrator.settings
    agent = BrowserApplyAgent(
        orchestrator.repo,
        settings,
        orchestrator.profile,
        orchestrator.resolver,
        orchestrator.gate,
        orchestrator.bus,
        cdp_url=settings.browser.cdp_url,
        memory=orchestrator.qualification_memory,
    )
    return SubmitWorker(orchestrator.repo, settings, agent, orchestrator.bus)


def _worker_interval() -> float:
    """Intervalo dos loops contínuos (configurável sem tocar no YAML)."""
    import os

    try:
        return max(1.0, float(os.getenv("AI_JOB_WORKER_INTERVAL_SECONDS", "300")))
    except ValueError:
        return 300.0


def _attach_progress(bus: EventBus) -> None:
    """Mostra progresso linha a linha quando não há painel Live ativo."""

    def on_qualification(event: Event) -> None:
        data = event.payload
        console.print(
            f"[dim]Analisadas {data['current']}/{data['total']}[/dim] "
            f"— {data['company']} · {data['title']} "
            f"[[cyan]{data['decision']}[/cyan]]"
        )

    def on_search_started(event: Event) -> None:
        data = event.payload
        console.print(
            f"[bold]Busca {data['current']}/{data['total']}[/bold] "
            f"— {data['query']} ({data['locations']} localidades)"
        )

    def on_location(event: Event) -> None:
        data = event.payload
        console.print(
            f"  [dim]↳ {data['location_index']}/{data['location_total']} "
            f"{data['location']}[/dim] "
            f"— cards={data['cards']} recentes={data['recent']} "
            f"novas={data['new_saved']}"
        )

    def on_search(event: Event) -> None:
        data = event.payload
        console.print(
            f"[green]Busca {data['current']}/{data['total']} concluída[/green] "
            f"— novas={data['new']} acumulado={data['cumulative']}"
        )

    bus.subscribe("qualification.progress", on_qualification)
    bus.subscribe("search.started", on_search_started)
    bus.subscribe("browser.location", on_location)
    bus.subscribe("search.progress", on_search)


@app.command()
def init() -> None:
    """Cria config.yaml a partir do exemplo."""
    settings = _settings()
    origin = settings.root / "config" / "config.example.yaml"
    target = settings.root / "config" / "config.yaml"
    if target.exists():
        console.print(f"[yellow]Já existe: {target}[/yellow]")
        return
    shutil.copy(origin, target)
    console.print(f"[green]Criado: {target}[/green]")


@app.command()
def status() -> None:
    """Mostra o estado atual do agente."""
    data = _orchestrator().status()
    table = Table(title="AGENT STATUS", show_header=False)
    table.add_column(style="bold cyan")
    table.add_column()
    for key, value in data.items():
        table.add_row(key, json.dumps(value, ensure_ascii=False, default=str))
    console.print(table)


@app.command()
def progress() -> None:
    """Mostra o progresso em tempo real da busca pública (data/status.json)."""
    settings = _settings()
    path = Path(settings.resolve(settings.storage.control_file)).with_name("status.json")
    if not path.exists():
        console.print("[yellow]Nenhum progresso registrado ainda.[/yellow]")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        console.print("[red]Não foi possível ler o progresso.[/red]")
        return
    table = Table(title="PROGRESSO DA BUSCA", show_header=False)
    table.add_column(style="bold cyan")
    table.add_column()
    table.add_row("busca", f"{data.get('query_index')}/{data.get('query_total')}")
    table.add_row("trace_id", str(data.get("trace_id", "") or "—"))
    table.add_row("termo", str(data.get("query", "")))
    table.add_row(
        "localidade",
        f"{data.get('location_index')}/{data.get('location_total')} "
        f"{data.get('location', '')}",
    )
    table.add_row("página", str(data.get("page", 0)))
    table.add_row("cards na última", str(data.get("cards_last", 0)))
    table.add_row("vagas recentes únicas", str(data.get("recent_unique", 0)))
    table.add_row("novas salvas", str(data.get("new_saved", 0)))
    table.add_row("atualizado em", str(data.get("updated_at", "")))
    console.print(table)


@app.command()
def device(
    set_unlock: bool = typer.Option(False, "--set-unlock", help="Cadastrar senha de desbloqueio"),
    delete_unlock: bool = typer.Option(False, "--delete-unlock", help="Remover senha salva"),
    unlock: bool = typer.Option(False, "--unlock", help="Testar desbloqueio agora"),
) -> None:
    """Verifica o dispositivo conectado e o ADB."""
    orchestrator = _orchestrator()
    store = orchestrator.adb.credentials
    if set_unlock:
        secret = typer.prompt("Senha de desbloqueio", hide_input=True)
        store.set_unlock_secret(secret)
        console.print("[green]Senha salva no cofre do Windows.[/green]")
        return
    if delete_unlock:
        store.delete_unlock_secret()
        console.print("[yellow]Senha removida.[/yellow]")
        return
    data = orchestrator.status()
    console.print(
        Panel(
            f"Dispositivo: {data['device']}\n"
            f"LinkedIn instalado: {data['linkedin_instalado']}\n"
            f"Senha de desbloqueio: {data['unlock_secret']}\n"
            f"ADB: {data['adb']}",
            title="DEVICE",
        )
    )
    if unlock:
        orchestrator.adb.require_device()
        changed = orchestrator.adb.unlock_if_needed()
        console.print("Tela desbloqueada." if changed else "Dispositivo já estava desbloqueado.")


@app.command("portal-auth")
def portal_auth(
    set_credentials: bool = typer.Option(False, "--set", help="Cadastrar usuário/senha"),
    delete_credentials: bool = typer.Option(False, "--delete", help="Remover credenciais"),
    domain: str = typer.Option("", "--domain", help="Domínio real do portal"),
    username: str = typer.Option("", "--username", help="Usuário (opcional; pode ser digitado depois)"),
) -> None:
    """Gerencia credenciais de portais conhecidos no cofre do Windows.

    A senha é lida com echo desligado e nunca aparece em log, banco ou config.
    """
    store = PortalCredentialStore()
    if set_credentials:
        if not domain:
            console.print("[red]Informe --domain (ex.: applicants.bairesdev.com).[/red]")
            raise typer.Exit(code=1)
        usuario = username or typer.prompt("Usuário")
        senha = typer.prompt("Senha", hide_input=True)
        store.set(domain, usuario, senha)
        console.print(f"[green]Credenciais de {domain} salvas no cofre do Windows.[/green]")
        return
    if delete_credentials:
        if not domain:
            console.print("[red]Informe --domain para remover.[/red]")
            raise typer.Exit(code=1)
        store.delete(domain)
        console.print(f"[yellow]Credenciais de {domain} removidas.[/yellow]")
        return

    dominios = list(_settings().application.portal_auth)
    if domain:
        dominios = [domain]
    tabela = Table(title="PORTAL AUTH")
    tabela.add_column("domínio")
    tabela.add_column("credenciais")
    for item in dominios:
        tabela.add_row(item, "cadastradas" if store.has(item) else "ausentes")
    console.print(tabela)


@app.command("device-pending")
def device_pending(
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """Lista candidaturas simplificadas aguardando a conexão do celular."""
    orchestrator = _orchestrator()
    jobs = orchestrator.pending_device_jobs(limit=limit)
    if not jobs:
        console.print("[green]Nenhuma candidatura aguardando o celular.[/green]")
        return
    table = Table(title=f"AGUARDANDO SMART PHONE ({len(jobs)})")
    for column in ("Trace ID", "ID", "Empresa", "Título", "Região", "Publicada"):
        table.add_column(column)
    for job in jobs:
        age = ""
        if job.posted_at is not None:
            minutes = max(0, int((datetime.now() - job.posted_at).total_seconds() / 60))
            age = f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"
        table.add_row(
            (job.trace_id or "—")[:12],
            job.external_id or "—",
            job.company[:28],
            job.title[:38],
            job.region,
            age,
        )
    console.print(table)
    console.print(
        "[yellow]Conecte o celular e rode 'agent device-apply' "
        "(ou use --watch para processar automaticamente).[/yellow]"
    )


@app.command("device-apply")
def device_apply_cmd(
    max_jobs: int = typer.Option(10, "--max-jobs", "-n"),
    watch: bool = typer.Option(
        False, "--watch", help="Fica aguardando o celular e processa a fila automaticamente"
    ),
    interval: int = typer.Option(15, "--interval", help="Segundos entre verificações no modo --watch"),
) -> None:
    """Processa a fila Easy Apply no celular conectado (aprovação já registrada)."""
    orchestrator = _orchestrator()
    while True:
        submitted, pending = orchestrator.process_device_queue(max_jobs=max_jobs)
        if not watch:
            console.print(
                f"Enviadas agora: {submitted} — ainda aguardando: {pending}"
            )
            return
        if pending == 0:
            console.print("[green]Fila zerada. Saindo do modo --watch.[/green]")
            return
        console.print(
            f"[dim]Aguardando celular… {pending} vaga(s) na fila "
            f"(enviadas nesta rodada: {submitted})[/dim]"
        )
        time.sleep(interval)


@app.command("probe-easy-apply")
def probe_easy_apply_cmd(
    limit: int = typer.Option(
        1, "--limit", "-n",
        help="Quantas vagas sondar nesta rodada (uma aba por vaga, aberta e fechada).",
    ),
) -> None:
    """Sonda Easy Apply no navegador LOGADO (CDP), uma vaga por vez.

    Abre cada vaga qualificada ainda não analisada em nova aba, verifica o
    botão no DOM e fecha a aba. Grava True/False no banco. NÃO envia nada.
    """
    orchestrator = _orchestrator()
    yes, no = orchestrator.probe_easy_apply_logged(limit=limit)
    console.print(
        f"Sondadas: {yes + no} | com Easy Apply: [green]{yes}[/green] | "
        f"sem: {no}"
    )


@app.command("dependency-done")
def dependency_done(
    dependency_id: str | None = typer.Argument(None, help="ID da dependência"),
    opportunity: str | None = typer.Option(None, "--opportunity", help="ID da vaga"),
    kind: str | None = typer.Option(None, "--kind", help="Tipo: video_self_intro, camera_test…"),
    note: str = typer.Option("", "--note", help="Observação curta do check"),
) -> None:
    """Dá o check no banco após vídeo/candidatura feita pelo próprio usuário."""
    if not dependency_id and not (opportunity and kind):
        console.print(
            "[red]Informe o ID da dependência, ou --opportunity com --kind.[/red]"
        )
        raise typer.Exit(code=1)
    dependency = _orchestrator().complete_dependency(
        dependency_id, opportunity_id=opportunity, kind=kind, note=note
    )
    if dependency is None:
        console.print("[red]Dependência não encontrada.[/red]")
        raise typer.Exit(code=1)
    console.print(
        f"[green]Check registrado:[/green] {dependency.kind} — "
        f"{dependency.company} · {dependency.title}"
    )


@app.command()
def profile() -> None:
    """Exibe o perfil profissional carregado."""
    agent = _orchestrator()
    data = agent.profile.model_dump(mode="json")
    console.print(
        Panel(
            json.dumps(
                {
                    "personal": data["personal"],
                    "current_title": data["current_title"],
                    "years_experience": data["years_experience"],
                    "technologies": sorted(agent.profile.all_technologies()),
                    "certifications": [c["name"] for c in data["certifications"]],
                    "unverified_claims": data["unverified_claims"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            title="CANDIDATE PROFILE",
        )
    )


@app.command()
def search(
    source: str | None = typer.Option(
        None, "--source", help="Fonte da busca: browser (padrão) ou android"
    ),
    no_qualify: bool = typer.Option(
        False,
        "--no-qualify",
        help="Só descobre vagas; não inicia a qualificação em paralelo",
    ),
) -> None:
    """Executa apenas a pesquisa de vagas."""
    settings = _settings()
    if source:
        if source not in {"browser", "android"}:
            raise typer.BadParameter("use --source browser ou --source android")
        settings = settings.model_copy(update={"search": settings.search.model_copy(
            update={"source": source}
        )})
    bus = EventBus()
    _attach_progress(bus)
    orchestrator = _orchestrator(bus, settings)
    state = orchestrator.run(
        phase="search", max_applications=0, qualify_live=not no_qualify
    )
    total = orchestrator.repo.stats()["jobs_total"]
    if state.errors:
        console.print(f"Status: {state.status.value} — {state.errors[0]}")
    console.print(f"Pesquisa concluída: {total} vagas no banco.")


@app.command("discovery-run")
def discovery_run(
    slot: str = typer.Option(
        "auto", "--slot", help="first | regular | auto | horário literal (10:00)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Calcula a janela e grava o watermark, sem buscar"
    ),
) -> None:
    """Executa uma rodada isolada de população da base (sem LLM, sem CDP)."""
    settings = _settings()
    bus = EventBus()
    _attach_progress(bus)
    result = DiscoveryAgent(settings, bus).run(slot, dry_run=dry_run)
    console.print(
        f"[green]Discovery concluído[/green] — {result.describe()}"
        if not dry_run
        else f"[yellow]Janela calculada[/yellow] — {result.describe()}"
    )


@app.command("discovery-serve")
def discovery_serve() -> None:
    """Processo contínuo que dispara os slots configurados no timezone local.

    Ao final de cada slot, o classificador consulta o banco e publica no outbox
    as vagas aprovadas. Este serviço roda na DGX e NÃO tem o Chrome logado do
    operador: a preparação das candidaturas é feita pela rotina separada
    ``application-serve`` na máquina local, com o mesmo ``AI_JOB_DATABASE_URL``.
    """
    settings = _settings()
    cfg = settings.discovery_schedule
    orchestrator: Orchestrator | None = None

    def run_slot(nome: str) -> None:
        nonlocal orchestrator
        console.print(f"[bold]Discovery[/bold] — slot {nome}")
        DiscoveryAgent(settings).run(nome)
        try:
            orchestrator = orchestrator or _orchestrator(settings=settings)
            worker = _classification_worker(orchestrator)
        except Exception as exc:
            console.print(f"[yellow]Classificação indisponível:[/yellow] {exc}")
            return
        try:
            resultado = worker.run()
            console.print(f"[bold]Classificação[/bold] — {resultado}")
        except Exception as exc:
            console.print(f"[red]Classificação falhou:[/red] {exc}")

    LocalScheduler(cfg.timezone, cfg.slots, run_slot).run_forever()


@app.command("classification-run")
def classification_run(
    limit: int = typer.Option(0, "--limit", help="Máximo de vagas nesta rodada (0 = todas)."),
) -> None:
    """Classifica o banco inteiro pendente (portais conhecidos primeiro)."""
    worker = _classification_worker(_orchestrator())
    counters = worker.run(limit or None)
    console.print(json.dumps(counters, ensure_ascii=False))


@app.command("classification-serve")
def classification_serve() -> None:
    """Loop contínuo do agente de classificação, independente do discovery."""
    orchestrator = _orchestrator()
    worker = _classification_worker(orchestrator)
    run_worker_loop(
        orchestrator.repo,
        name="classification",
        callback=worker.run,
        interval_seconds=_worker_interval(),
    )


@app.command("application-prepare-run")
def application_prepare_run(
    limit: int = typer.Option(0, "--limit", help="Máximo de vagas nesta rodada (0 = todas)."),
) -> None:
    """Prepara candidaturas de vagas QUALIFIED e para em PENDING_APPROVAL."""
    worker = _application_worker(_orchestrator())
    counters = worker.run(limit or None)
    console.print(json.dumps(counters, ensure_ascii=False))


@app.command("application-serve")
def application_serve() -> None:
    """Loop contínuo do agente de aplicação; nunca envia sem APPROVED humano."""
    orchestrator = _orchestrator()
    worker = _application_worker(orchestrator)
    run_worker_loop(
        orchestrator.repo,
        name="application",
        callback=worker.run,
        interval_seconds=_worker_interval(),
    )


@app.command("apply-submit-run")
def apply_submit_run(
    limit: int = typer.Option(0, "--limit", help="Máximo de envios nesta rodada (0 = todos)."),
) -> None:
    """Consome checks aprovados na PWA e envia após revalidação determinística."""
    worker = _submit_worker(_orchestrator())
    counters = worker.run(limit or None)
    console.print(json.dumps(counters, ensure_ascii=False))


@app.command("apply-submit-serve")
def apply_submit_serve(
    interval: float = typer.Option(
        30.0, "--interval", help="Intervalo de verificação do outbox de aprovações."
    ),
) -> None:
    """Loop do envio autorizado pela PWA; usa a mesma trava da aplicação."""
    orchestrator = _orchestrator()
    worker = _submit_worker(orchestrator)
    run_worker_loop(
        orchestrator.repo,
        name="application",
        callback=worker.run,
        interval_seconds=max(1.0, interval),
    )


@app.command("review-serve")
def review_serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind do servidor da PWA."),
    port: int = typer.Option(8765, "--port", help="Porta local da PWA."),
) -> None:
    """Sobe a plataforma intermediária de revisão das vagas classificadas."""
    from .web.review_app import run_server

    run_server(host, port)


@app.command()
def reclassify(
    limit: int = typer.Option(
        0, "--limit", "-n", help="0 = todos os alvos (padrão: 0)"
    ),
    statuses: str = typer.Option(
        "DISCOVERED,QUALIFIED",
        "--statuses",
        help="Statuses a reanalisar, separados por vírgula",
    ),
    ids: str = typer.Option(
        "", "--ids", help="IDs específicos (separados por vírgula); ignora --statuses"
    ),
    trace_id: str = typer.Option(
        "",
        "--trace-id",
        help="Reclassifica somente vagas deste Trace ID; ignora --statuses e --ids",
    ),
    all_statuses: bool = typer.Option(
        False, "--all", help="Inclui rejeitadas e vencidas; ignora --statuses"
    ),
    ignore_recency: bool = typer.Option(
        False,
        "--ignore-recency",
        help="Ignora a janela de 6h nesta reanálise (apenas auditoria da base)",
    ),
) -> None:
    """Reaplica as regras atuais às vagas salvas, sem submeter candidaturas."""
    try:
        alvos = [JobStatus(item.strip().upper()) for item in statuses.split(",") if item.strip()]
    except ValueError as exc:
        console.print(f"[red]Status inválido: {exc}[/red]")
        raise typer.Exit(code=1)
    bus = EventBus()
    _attach_progress(bus)
    orchestrator = _orchestrator(bus)
    aprovadas = orchestrator.reclassify_jobs(
        statuses=alvos,
        limit=limit or None,
        job_ids=[item.strip() for item in ids.split(",") if item.strip()] or None,
        trace_id=trace_id.strip() or None,
        all_statuses=all_statuses,
        ignore_recency=ignore_recency,
    )
    if orchestrator.qualification.last_error:
        console.print(f"[yellow]Atenção: {orchestrator.qualification.last_error}[/yellow]")
    console.print(
        f"[green]Reclassificação concluída: {aprovadas} vaga(s) qualificada(s).[/green]"
    )


@app.command("fast-lane")
def fast_lane(
    company: str = typer.Option(
        "BairesDev", "--company", "-c", help="Nome do portal confiável"
    ),
    statuses: str = typer.Option(
        "DISCOVERED", "--statuses", help="Statuses a classificar, separados por vírgula"
    ),
    limit: int = typer.Option(
        0, "--limit", "-n", help="0 = todas as vagas elegíveis (padrão)"
    ),
) -> None:
    """Classifica vagas de portal confiável com validação determinística, sem LLM."""
    try:
        alvos = [
            JobStatus(item.strip().upper()) for item in statuses.split(",") if item.strip()
        ]
    except ValueError as exc:
        console.print(f"[red]Status inválido: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    settings = _settings()
    repo = Repository(
        settings.resolve(settings.storage.database),
        known_portals=settings.application.known_portals,
    )
    agent = FastLaneAgent(repo, settings, company_name=company)
    try:
        counters = agent.run(statuses=alvos, limit=limit or None)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[bold]Fast lane {company}[/bold]: {counters['total']} vaga(s) · "
        f"[green]{counters['qualified']} aprovada(s)[/green] · "
        f"[yellow]{counters['rejected']} reprovada(s)[/yellow]."
    )


@app.command("normalize-locations")
def normalize_locations() -> None:
    """Preenche 'País - localização' nas vagas salvas, sem reanalisar nada."""
    settings = _settings()
    repo = Repository(
        settings.resolve(settings.storage.database),
        known_portals=settings.application.known_portals,
    )
    total = atualizados = 0
    for job in repo.list_jobs():
        total += 1
        normalized = normalize_location(job.location)
        if job.location_normalized != normalized:
            job.location_normalized = normalized
            repo.update_job(job)
            atualizados += 1
    console.print(
        f"[green]Localizações normalizadas: {atualizados}/{total} vagas atualizadas.[/green]"
    )


@app.command("sanitize-restriction-details")
def sanitize_restriction_details() -> None:
    """Reescreve detalhes semânticos antigos sem repetir o perfil do candidato."""
    settings = _settings()
    repo = Repository(settings.resolve(settings.storage.database))
    total = alterados = 0
    for job in repo.list_jobs():
        details = job.restriction_details or ""
        assessment = job.fit_assessment
        if (
            not details.startswith("análise:")
            or assessment is None
            or assessment.language_fit != "misaligned"
        ):
            continue
        total += 1
        novo = f"análise: {job_side_language_reason(assessment)}"
        if novo != details:
            job.restriction_details = novo
            repo.update_job(job)
            alterados += 1
    console.print(
        f"[green]Detalhes semânticos: {alterados}/{total} reescritos.[/green]"
    )


@app.command("link-companies")
def link_companies() -> None:
    """Vincula ``company_id`` nas vagas antigas, sem reanalisar nada."""
    settings = _settings()
    repo = Repository(
        settings.resolve(settings.storage.database),
        known_portals=settings.application.known_portals,
    )
    total = vinculadas = 0
    for job in repo.list_jobs():
        total += 1
        if not job.company_id:
            repo.update_job(job)  # update_job cria/busca a empresa e vincula.
            vinculadas += 1
    console.print(
        f"[green]Empresas vinculadas: {vinculadas}/{total} vagas atualizadas.[/green]"
    )


@app.command()
def apply(
    max_applications: int = typer.Option(10, "--max-applications", "-n"),
) -> None:
    """Qualifica e prepara candidaturas (com aprovação antes do envio)."""
    bus = EventBus()
    _attach_progress(bus)
    state = _orchestrator(bus).run(phase="apply", max_applications=max_applications)
    console.print(f"Execução: {state.status.value} — enviadas: {state.applications_submitted}")


@app.command()
def run(
    max_applications: int = typer.Option(10, "--max-applications", "-n"),
) -> None:
    """Executa o fluxo completo com painel em tempo real."""
    bus = EventBus()
    orchestrator = _orchestrator(bus)
    dashboard = Dashboard(bus, orchestrator.status())
    dashboard.start()
    try:
        state = orchestrator.run(max_applications=max_applications)
    finally:
        dashboard.stop()
    if state.errors:
        console.print("[red]Erros:[/red]")
        for error in state.errors:
            console.print(f"  - {error}")
    console.print(
        f"[green]Finalizado: {state.status.value} | "
        f"descobertas={state.jobs_discovered} qualificadas={state.jobs_qualified} "
        f"enviadas={state.applications_submitted}[/green]"
    )


@app.command()
def jobs(
    limit: int = typer.Option(30, "--limit", "-n"),
    status: str = typer.Option("QUALIFIED", "--status", "-s"),
) -> None:
    """Lista vagas com região, canal, recência e indicador de moeda."""
    from .agents.orchestrator import sort_application_queue
    from .core.config import Settings as _Settings

    _settings = _Settings.load()
    orchestrator = _orchestrator()
    try:
        wanted = JobStatus(status.upper())
    except ValueError:
        console.print(f"[red]Status inválido: {status}[/red]")
        raise typer.Exit(code=1)
    items = orchestrator.repo.list_jobs(status=wanted, limit=limit)
    channel_priority = {
        name: index for index, name in enumerate(_settings.application.channel_priority)
    }
    region_priority = {
        name: index for index, name in enumerate(_settings.application.region_priority)
    }
    items = sort_application_queue(items, channel_priority, region_priority)

    table = Table(title=f"VAGAS — {status.upper()}")
    for column in (
        "Pont.", "Região", "Canal", "Moeda", "Salário", "Aplicada", "Restrição",
        "Publicada", "Empresa", "Título", "Local",
    ):
        table.add_column(column)
    for job in items:
        age = ""
        if job.posted_at is not None:
            minutes = max(0, int((datetime.now() - job.posted_at).total_seconds() / 60))
            age = f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"
        currency = job.currency_signal or "—"
        if job.multi_currency:
            currency += " (multi)"
        salary = job.salary_text or ("sim" if job.salary_disclosed else "—")
        applied = "sim" if job.has_applied else "não"
        restriction = "sim" if job.has_restriction else "—"
        table.add_row(
            str(job.score if job.score is not None else "—"),
            job.region,
            job.channel,
            currency,
            salary[:22],
            applied,
            restriction,
            age,
            job.company[:32],
            job.title[:40],
            job.location[:32],
        )
    console.print(table)


@app.command()
def prune(
    apply: bool = typer.Option(False, "--apply", help="Marca EXPIRED de verdade (padrão: simulação)"),
    hours: float = typer.Option(
        0.0, "--hours", help="Janela em horas; 0 usa search.expired_after_hours"
    ),
) -> None:
    """Marca como EXPIRED vagas fora da janela — NUNCA exclui o histórico."""
    settings = _settings()
    orchestrator = _orchestrator()
    window = hours or settings.search.expired_after_hours
    cutoff = datetime.now() - timedelta(hours=window)
    stale = orchestrator.repo.expire_stale_jobs(cutoff, apply=apply)
    verb = "Marcadas como EXPIRED" if apply else "Seriam marcadas como EXPIRED"
    console.print(
        f"{verb} {len(stale)} vagas fora da janela de {window}h. "
        f"Nenhuma vaga é excluída — o histórico fica para análise temporal."
    )
    if not apply:
        console.print("[dim]Use --apply para registrar no banco.[/dim]")


@app.command()
def report() -> None:
    """Gera relatório persistente da execução."""
    data = _orchestrator().report()
    console.print(
        Panel(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            title="EXECUTION REPORT",
        )
    )


@app.command()
def pause() -> None:
    """Solicita pausa da execução em andamento."""
    _orchestrator().pause()
    console.print("[yellow]Pausa solicitada.[/yellow]")


@app.command()
def resume() -> None:
    """Retoma a execução pausada."""
    _orchestrator().resume()
    console.print("[green]Execução retomada.[/green]")


@app.command()
def stop() -> None:
    """Interrompe a execução com segurança."""
    _orchestrator().stop()
    console.print("[red]Interrupção solicitada.[/red]")


@app.command("gmail-auth")
def gmail_auth() -> None:
    """Autentica o Gmail com OAuth (leitura da caixa de entrada e envio)."""
    orchestrator = _orchestrator()
    orchestrator.gmail.service()
    console.print("[green]Gmail autenticado; token salvo no cofre do Windows.[/green]")


@app.command()
def reminder(
    force: bool = typer.Option(
        False, "--force", help="Envia mesmo fora do horário/já enviado hoje"
    ),
) -> None:
    """Envia manualmente o lembrete diário consolidado de pendências/erros."""
    orchestrator = _orchestrator()
    sent = orchestrator.notifications.send_reminder(force=force)
    console.print(
        "[green]Lembrete enviado.[/green]" if sent
        else "[yellow]Nada enviado (sem pendências, fora do horário ou já enviado hoje).[/yellow]"
    )


@app.command()
def trace(trace_id: str) -> None:
    """Rastreia uma execução: vagas, candidaturas, dependências e e-mails."""
    orchestrator = _orchestrator()
    repo = orchestrator.repo
    execution = repo.get_execution_by_trace(trace_id)
    jobs = repo.list_jobs(trace_id=trace_id)
    applications = repo.list_applications(trace_id=trace_id)
    dependencies = repo.list_dependencies(trace_id=trace_id)
    emails = repo.list_notifications(trace_id=trace_id)
    panel = "\n".join(
        [
            f"Execução: {execution.status if execution else 'não encontrada'}",
            f"Vagas: {len(jobs)}",
            f"Candidaturas: {len(applications)}",
            f"Dependências: {len(dependencies)}",
            f"E-mails: {len(emails)}",
        ]
    )
    console.print(Panel(panel, title=f"TRACE {trace_id}"))

    if jobs:
        table = Table(title="VAGAS DO TRACE")
        for column in ("ID", "Status", "Empresa", "Título"):
            table.add_column(column)
        for job in jobs:
            table.add_row(job.id, job.status.value, job.company[:30], job.title[:40])
        console.print(table)

    if emails:
        table = Table(title="E-MAILS DO TRACE")
        for column in ("Quando", "Tipo", "Gmail ID", "Assunto"):
            table.add_column(column)
        for email in emails:
            table.add_row(
                email["sent_at"], email["kind"], email["message_id"], email["subject"][:50]
            )
        console.print(table)


@app.command()
def doctor() -> None:
    """Diagnóstico rápido de dependências, ADB e LLM."""
    orchestrator = _orchestrator()
    data = orchestrator.status()
    console.print(
        Panel(
            f"ADB: {data['adb']}\nDispositivo: {data['device']}\nLLM: {data['llm']}\n"
            f"Perfil: {data['profile']}",
            title="DOCTOR",
        )
    )
    try:
        answer = orchestrator.llm.chat(
            [{"role": "user", "content": "Responda apenas: OK"}]
        )
        console.print(f"[green]LLM respondeu: {answer[:40]}[/green]")
    except Exception as exc:
        console.print(f"[red]LLM falhou: {exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command("profile-refresh-linkedin")
def profile_refresh_linkedin(
    url: str = typer.Option("", "--url", help="URL do perfil (padrão: a do perfil salvo)"),
) -> None:
    """Atualiza o perfil completo com as skills atuais do LinkedIn logado."""
    orchestrator = _orchestrator()
    skills = orchestrator.refresh_profile_from_linkedin(url)
    if not skills:
        console.print("[yellow]Nenhuma skill lida. Verifique o Chrome debug logado.[/yellow]")
        raise typer.Exit(code=1)
    console.print(f"[green]{len(skills)} skills sincronizadas do LinkedIn.[/green]")
    for skill in skills:
        console.print(f"  • {skill}")


@app.command("browser-apply")
def browser_apply_cmd(
    ids: list[str] = typer.Option(
        [], "--id", "--ids", help="ID interno da vaga no banco (repetível)."
    ),
    prepare_only: bool = typer.Option(
        False, "--prepare-only", help="Só abre/preenche e registra a aprovação pendente."
    ),
    submit_only: bool = typer.Option(
        False, "--submit-only", help="Só envia vagas já preparadas (ainda pede APPROVED)."
    ),
) -> None:
    """Candidatura agêntica no Chrome logado via CDP, com APPROVED por vaga.

    Nunca abre outro perfil, nunca maximiza/fecha a janela e nunca envia sem a
    confirmação humana persistida pelo ApprovalGate.
    """
    if not ids:
        console.print("[red]Informe ao menos uma vaga com --id/--ids.[/red]")
        raise typer.Exit(code=1)
    if prepare_only and submit_only:
        console.print("[red]--prepare-only e --submit-only são mutually exclusivos.[/red]")
        raise typer.Exit(code=1)

    orchestrator = _orchestrator()
    settings = orchestrator.settings
    repo = orchestrator.repo
    agent = BrowserApplyAgent(
        repo,
        settings,
        orchestrator.profile,
        orchestrator.resolver,
        orchestrator.gate,
        orchestrator.bus,
        cdp_url=settings.browser.cdp_url,
        memory=orchestrator.qualification_memory,
    )
    prepared = submitted = rejected = failed = already = 0

    for job_id in ids:
        job = repo.get_job(job_id)
        if job is None:
            console.print(f"[red]Vaga não encontrada: {job_id}[/red]")
            failed += 1
            continue
        console.print(
            Panel(
                f"Empresa: {job.company}\nCargo: {job.title}\n"
                f"Score: {job.score}/100 · Status: {job.status.value}\n"
                f"Localização: {job.location_normalized or job.location}",
                title=f"BROWSER APPLY · {job.id[:12]}",
            )
        )
        try:
            if submit_only:
                approval = repo.get_approval_for_job(job.id)
                if approval is None or approval.status != "PENDING":
                    raise ValidationError("sem aprovação PENDING; rode --prepare-only primeiro")
                if repo.has_application_for(job.id):
                    raise ValidationError("candidatura já registrada para esta vaga")
            else:
                approval = agent.prepare(job)
                if approval is None:
                    already += 1
                    console.print(
                        f"[cyan]Já candidaturada:[/cyan] {job.company} — {job.title}\n"
                        "[dim]Candidatura existente registrada; nenhum envio nem aprovação criado.[/dim]"
                    )
                    continue
            prepared += 1

            if prepare_only:
                console.print(
                    f"[cyan]Preparada:[/cyan] {len(approval.answers)} campo(s), "
                    f"aprovação {approval.id[:12]} pendente."
                )
                continue

            if not agent.approve(job, approval):
                console.print("[yellow]Envio reprovado pelo operador.[/yellow]")
                rejected += 1
                continue

            application = agent.submit(job, approval, confirmed=True)
            if application is None:
                rejected += 1
                continue
            submitted += 1
            console.print(
                f"[green]Enviada:[/green] {job.company} — {job.title}\n"
                f"[dim]Portal: {application.portal_url or '—'}[/dim]"
            )
        except HumanActionRequired as exc:
            job.status = JobStatus.REQUIRES_HUMAN_ACTION
            repo.update_job(job)
            agent.memory.record_outcome(job, "REQUIRES_HUMAN_ACTION", str(exc))
            console.print(f"[red]Ação humana necessária:[/red] {exc}")
            failed += 1
        except BlockedByPlatformError as exc:
            job.status = JobStatus.FAILED
            repo.update_job(job)
            agent.memory.record_outcome(job, "FAILED", str(exc))
            console.print(f"[red]Bloqueio da plataforma:[/red] {exc}")
            failed += 1
        except AgentError as exc:
            agent.memory.record_outcome(job, "FAILED", str(exc))
            console.print(f"[red]Falha:[/red] {exc}")
            failed += 1

    console.print(
        f"Preparadas: {prepared} | enviadas: [green]{submitted}[/green] | "
        f"reprovadas: {rejected} | falhas: {failed} | já candidaturadas: {already}"
    )


company_app = typer.Typer(help="Supressão temporária de empresas (não é blacklist)")


@company_app.command("list")
def company_list() -> None:
    """Lista empresas e suas supressões temporárias ativas."""
    settings = _settings()
    repo = Repository(settings.resolve(settings.storage.database))
    table = Table(title="EMPRESAS")
    for column in ("Empresa", "Suprimida até", "Motivo"):
        table.add_column(column)
    for company in repo.list_companies():
        ate = company.suppressed_until.strftime("%Y-%m-%d %H:%M") if company.suppressed_until else "—"
        table.add_row(company.name, ate, company.suppression_reason[:80] or "—")
    console.print(table)


@company_app.command("check")
def company_check(
    name: str,
    days: int = typer.Option(30, "--days", help="Usado apenas no texto do relatório"),
) -> None:
    """Confere o padrão de restrição em outras vagas da empresa no LinkedIn."""
    settings = _settings()
    repo = Repository(settings.resolve(settings.storage.database))
    agente = CompanySuppressionAgent(settings, repo, LLMRouter(settings))
    resultado = agente.check(name)
    console.print(
        Panel(
            f"Empresa: {resultado.company}\n"
            f"Vagas confirmadas: {resultado.checked}\n"
            f"Com restrição geográfica/visto: {resultado.matching}\n"
            f"Padrão confirmado: {'SIM' if resultado.confirmed else 'NÃO'}\n"
            f"Janela de supressão sugerida: {days}d",
            title="COMPANY CHECK",
        )
    )
    for linha in resultado.details:
        console.print(f"  {linha}")


@company_app.command("suppress")
def company_suppress(
    name: str,
    days: int = typer.Option(30, "--days", help="Dias de supressão"),
    reason: str = typer.Option("", "--reason", help="Motivo/evidência da restrição"),
    skip_check: bool = typer.Option(
        False, "--skip-check", help="Marca sem conferir outras vagas (uso manual)"
    ),
) -> None:
    """Marca uma empresa por 30 dias após conferir o padrão no LinkedIn."""
    settings = _settings()
    repo = Repository(settings.resolve(settings.storage.database))
    if skip_check:
        empresa = repo.suppress_company(name, days=days, reason=reason or "supressão manual")
        console.print(
            f"[yellow]{empresa.name} suprimida até "
            f"{empresa.suppressed_until:%Y-%m-%d %H:%M} (sem conferência).[/yellow]"
        )
        return

    agente = CompanySuppressionAgent(settings, repo, LLMRouter(settings))
    check = agente.check(name)
    if not check.confirmed:
        console.print(
            f"[yellow]Padrão NÃO confirmado ({check.matching}/{check.checked}); "
            f"nada foi marcado.[/yellow]"
        )
        for linha in check.details:
            console.print(f"  {linha}")
        raise typer.Exit(code=1)
    motivo = reason or (
        f"restrição geográfica/visto confirmada em {check.matching}/{check.checked} vagas"
    )
    empresa = repo.suppress_company(name, days=days, reason=motivo)
    console.print(
        f"[green]{empresa.name} suprimida até "
        f"{empresa.suppressed_until:%Y-%m-%d %H:%M}.[/green]"
    )


@company_app.command("clear")
def company_clear(name: str) -> None:
    """Remove a supressão antes do prazo (revisão manual)."""
    settings = _settings()
    repo = Repository(settings.resolve(settings.storage.database))
    empresa = repo.clear_company_suppression(name)
    if empresa is None:
        console.print(f"[yellow]Empresa não encontrada: {name}[/yellow]")
        return
    console.print(f"[green]Supressão removida de {empresa.name}.[/green]")


app.add_typer(company_app, name="company")


if __name__ == "__main__":
    app()
