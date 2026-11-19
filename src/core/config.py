"""Carregamento e validação da configuração (YAML -> Pydantic)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .errors import ConfigError


class AppConfig(BaseModel):
    name: str = "Local AI Job Application Agent"
    timezone: str = "America/Sao_Paulo"
    mode: Literal["assisted", "autonomous"] = "assisted"


class ProviderConfig(BaseModel):
    # Somente backends compatíveis com a API OpenAI. DGX é o único desta máquina.
    type: Literal["openai_compatible"] = "openai_compatible"
    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: int = 300
    disable_thinking: bool = True


class LLMConfig(BaseModel):
    primary: str = "dgx"
    # Nunca usar fallback: LM Studio/Ollama são proibidos nesta máquina.
    fallback: str | None = Field(default=None, exclude=True)
    temperature: float = 0.1
    max_retries: int = 2
    max_output_tokens: int = 2048
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)


class UnlockSecretConfig(BaseModel):
    store: Literal["keyring"] = "keyring"
    keyring_service: str = "local-ai-job-agent"
    keyring_username: str = "device-screen-unlock"


class DeviceConfig(BaseModel):
    adb_path: str = "adb"
    appium_url: str = "http://127.0.0.1:4723"
    linkedin_package: str = "com.linkedin.android"
    screenshot_policy: Literal["memory_only", "disabled"] = "memory_only"
    max_scrolls_per_search: int = 30
    action_delay_ms: int = 350
    use_visible_keyboard: bool = False
    allow_easy_apply: bool = True
    allow_external_portals: bool = True
    unlock_secret: UnlockSecretConfig = Field(default_factory=UnlockSecretConfig)


class BrowserConfig(BaseModel):
    """Leitura das páginas públicas e automação agêntica no Chrome logado."""

    source: str = "chrome"  # canal real do Playwright: chrome, msedge ou "" (bundled)
    headless: bool = True
    timeout_ms: int = 30000
    delay_ms: int = 1200  # pausa conservadora entre navegações
    # Endpoint CDP do perfil JÁ LOGADO. Nunca abrir nova janela/perfil.
    cdp_url: str = "http://127.0.0.1:9222"
    max_pages_per_query: int = 1
    fallback_to_android: bool = True
    drain_timeout_seconds: int = 3600  # drenagem da qualificação ao final da busca


class DiscoveryScheduleConfig(BaseModel):
    """Janela e horários do serviço isolado de população da base.

    ``reference_time`` é capturado UMA vez no início da execução e congelado:
    nenhuma query/localidade/página pode usar ``datetime.now()`` durante a busca.
    O cutoff é o mais antigo entre a janela configurada (com sobreposição) e o
    watermark da última execução bem-sucedida.
    """

    timezone: str = "America/Sao_Paulo"
    slots: list[str] = Field(
        default_factory=lambda: ["10:00", "12:30", "15:00", "18:30"]
    )
    first_slot: str = "10:00"
    linkedin_ui_filter: Literal["day", "week"] = "day"  # f_TPR nativo do LinkedIn
    first_run_hours: int = 12
    regular_hours: int = 3
    # Cobre o tempo gasto pela própria execução (40–60 min) e evita buraco
    # entre uma rodada e outra.
    overlap_buffer_minutes: int = 60
    use_last_watermark: bool = True
    # Se o cutoff ficar mais antigo que o filtro nativo, usa "week".
    auto_widen_time_filter: bool = True


class SearchConfig(BaseModel):
    source: Literal["browser", "android"] = "browser"
    queries: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    recency_hours: int = 6
    # Janela para classificar como EXPIRED (auditoria/histórico). Separada da
    # descoberta: na fase de testes, 6h descartava cedo demais o histórico.
    expired_after_hours: int = 24
    currencies: list[str] = Field(default_factory=lambda: ["USD", "EUR"])
    require_salary: bool = False
    remote_only: bool = True
    allow_national_remote: bool = True
    national_countries: list[str] = Field(default_factory=lambda: ["Brazil"])
    require_explicit_recency: bool = True
    max_jobs_per_query: int = 25


class WeightsConfig(BaseModel):
    """Pesos do score. Salário NÃO entra no score (moeda filtra à parte).

    Os 15 pontos antes reservados à compensação foram redistribuídos para
    technical (30->35) e experience (20->25) e international (10->15).
    """

    technical: int = 35
    seniority: int = 20
    experience: int = 25
    international: int = 15
    language: int = 5

    def total(self) -> int:
        return (
            self.technical
            + self.seniority
            + self.experience
            + self.international
            + self.language
        )


class RejectConfig(BaseModel):
    work_authorization_required: bool = True
    onsite_in_unsupported_country: bool = True
    unsupported_language: bool = True
    non_remote: bool = True
    # Moeda é critério: só USD/EUR. Vaga brasileira/em português sem
    # informação explícita de pagamento em USD/EUR é reprovada; internacional
    # sem moeda explícita é presumida elegível.
    salary_currency_mismatch: bool = True
    stale_posting: bool = True
    technology_mismatch: bool = True
    # Barreiras de elegibilidade (visto, idioma, residência) geram SKIP.
    eligibility_restriction: bool = True
    # Liderança explícita (tech lead/manager) nunca é alvo.
    explicit_leadership_role: bool = True
    # Só rejeita quando a vaga EXIGE domínio/setor ausente no perfil;
    # diferencial ("nice to have") nunca rejeita.
    missing_required_domain_experience: bool = True


class EligibilityConfig(BaseModel):
    """Restrições do candidato usadas na análise de cada vaga.

    Tudo aqui é informação fornecida pelo operador — o LLM nunca "inventa" que
    o candidato tem visto ou idioma que não esteja nesta configuração.
    """

    # Idiomas ACEITOS. Apenas a identidade do idioma importa: nível
    # (fluent/C1/C2/native) NUNCA barra — inglês técnico é suficiente na
    # prática. Francês, alemão etc. continuam sendo restrição.
    accepted_languages: list[str] = Field(
        default_factory=lambda: ["english", "portuguese", "spanish"]
    )
    # Legado: níveis por idioma. Mantido só para compatibilidade de config;
    # NÃO é mais usado para rejeitar vagas.
    languages: dict[str, str] = Field(
        default_factory=lambda: {
            "english": "intermediate",
            "portuguese": "native",
            "spanish": "intermediate",
        }
    )
    # Países onde pode trabalhar sem visto/sponsorship.
    work_authorization_countries: list[str] = Field(
        default_factory=lambda: ["brazil"]
    )
    visa_sponsorship_available: bool = False
    # Onde o candidato mora/pode morar — usado quando a vaga exige residência.
    residence_countries: list[str] = Field(default_factory=lambda: ["brazil"])
    residence_regions: list[str] = Field(default_factory=lambda: ["brazil"])
    # Quando a descrição não diz o suficiente, consulta o link público da empresa.
    enrich_from_company_page: bool = True


class CompanySuppressionConfig(BaseModel):
    """Supressão TEMPORÁRIA de empresas por restrição geográfica/visto.

    Não é blacklist e não atua na busca: a empresa só é sinalizada depois que
    a análise confirma a restrição em OUTRAS vagas do LinkedIn logado. O filtro
    real acontece antes de salvar o card descoberto no banco.
    """

    enabled: bool = True
    days: int = 30
    # Quantas vagas da empresa são amostradas para confirmar o padrão.
    sample_jobs: int = 3
    # Mínimo de vagas com o MESMO tipo de restrição para confirmar.
    min_matching: int = 2
    delay_ms: int = 1200


class QualificationConfig(BaseModel):
    min_score: int = 70
    min_technology_match: float = 0.5
    weights: WeightsConfig = Field(default_factory=WeightsConfig)
    reject: RejectConfig = Field(default_factory=RejectConfig)


class ApplicationConfig(BaseModel):
    """Canais de candidatura e prioridade de execução (menor índice = primeiro)."""

    known_portals: list[str] = Field(default_factory=lambda: ["BairesDev", "Strider"])
    channel_priority: list[str] = Field(
        default_factory=lambda: ["easy_apply", "known_portal", "external_portal"]
    )
    # Prioridade geográfica (menor índice = primeiro) aplicada antes do canal.
    region_priority: list[str] = Field(
        default_factory=lambda: [
            "americas_north", "australia", "new_zealand", "europe", "brazil", "other",
        ]
    )
    require_success_feedback: bool = True


class LimitsConfig(BaseModel):
    max_applications_per_day: int = 10
    max_applications_per_run: int = 10
    max_hours_per_run: int = 4


class ApprovalConfig(BaseModel):
    before_submit: bool = True
    always_human: list[str] = Field(default_factory=list)


class GmailConfig(BaseModel):
    enabled: bool = True
    scopes: list[str] = Field(
        default_factory=lambda: [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ]
    )
    credentials_file: str = "config/gmail_oauth.json"
    keyring_service: str = "local-ai-job-agent"
    keyring_username: str = "gmail-token"
    max_results: int = 25
    # "me" envia para a própria conta autenticada; troque se quiser outro destino.
    alert_recipient: str = "me"
    # Resumo de cada execução (sempre) + um único lembrete consolidado.
    send_run_summary: bool = True
    # Lembrete consolidado UMA vez por dia, no fim do dia (horário configurado).
    send_human_reminder: bool = True
    reminder_hour: int = 18
    # Prazo dentro dessa janela é destacado no e-mail; não antecipa o envio.
    reminder_before_due_hours: int = 24


class StorageConfig(BaseModel):
    database: str = "data/agent.db"
    log_dir: str = "data/logs"
    control_file: str = "data/control.json"


class ProfileConfig(BaseModel):
    cv_path: str = ""
    legacy_profile: str = "config/profile.legacy.yaml"
    legacy_knowledge: str = "data/conhecimento.legacy.yaml"
    # Documento local opcional de domínios em tecnologias para responder perguntas.
    tech_domains_doc: str = ""
    # Setores/domínios com experiência comprovada no CV. Usados APENAS para
    # verificar exigências explícitas da vaga — nunca como filtro de interesse.
    domain_experience: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    device: DeviceConfig = Field(default_factory=DeviceConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    discovery_schedule: DiscoveryScheduleConfig = Field(
        default_factory=DiscoveryScheduleConfig
    )
    search: SearchConfig = Field(default_factory=SearchConfig)
    qualification: QualificationConfig = Field(default_factory=QualificationConfig)
    eligibility: EligibilityConfig = Field(default_factory=EligibilityConfig)
    company_suppression: CompanySuppressionConfig = Field(
        default_factory=CompanySuppressionConfig
    )
    application: ApplicationConfig = Field(default_factory=ApplicationConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    gmail: GmailConfig = Field(default_factory=GmailConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    root: Path = Field(default_factory=Path.cwd, exclude=True)

    @classmethod
    def load(cls, root: Path | None = None, filename: str = "config.yaml") -> "Settings":
        root = (root or Path(__file__).resolve().parents[2]).resolve()
        path = root / "config" / filename
        if not path.exists():
            # Quando o pacote é instalado via pip, ``__file__`` fica em
            # site-packages. O working directory do container (/app) é a
            # fonte correta da configuração.
            cwd_root = Path.cwd().resolve()
            cwd_path = cwd_root / "config" / filename
            if cwd_path.exists():
                root, path = cwd_root, cwd_path
        if not path.exists():
            # Nunca cair no example: ele pode conter provedores proibidos.
            raise ConfigError(f"Configuração obrigatória ausente: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            # O discovery roda no DGX com o Chromium embutido do Playwright;
            # a máquina do operador continua usando o Chrome instalado.
            browser_source = os.getenv("AI_JOB_BROWSER_SOURCE")
            if browser_source is not None:
                data.setdefault("browser", {})["source"] = browser_source
            settings = cls.model_validate(data)
        except Exception as exc:  # pragma: no cover - fallback de diagnóstico
            raise ConfigError(f"Falha ao carregar {path}: {exc}") from exc
        settings.root = root
        return settings

    def resolve(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (self.root / path)
